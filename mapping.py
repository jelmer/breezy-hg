# Copyright (C) 2005, 2006, 2009 Canonical Ltd
# Copyright (C) 2008 Jelmer Vernooij <jelmer@samba.org>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""Mappings."""

import base64
import mercurial
from mercurial.node import (
    hex,
    bin,
    )
from mercurial.revlog import (
    hash as hghash,
    )

from breezy import (
    bencode,
    errors,
    foreign,
    osutils,
    revision as _mod_revision,
    trace,
    )

import urllib


def mode_kind(mode):
    """Determine the Bazaar inventory kind based on Unix file mode."""
    entry_kind = (mode & 0700000) / 0100000
    if entry_kind == 0:
        return 'directory'
    elif entry_kind == 1:
        file_kind = (mode & 070000) / 010000
        if file_kind == 0:
            return 'file'
        elif file_kind == 2:
            return 'symlink'
        elif file_kind == 6:
            return 'tree-reference'
        else:
            raise AssertionError(
                "Unknown file kind %d, perms=%o." % (file_kind, mode,))
    else:
        raise AssertionError(
            "Unknown kind, perms=%r." % (mode,))


def convert_converted_from(rev):
    """Convert a Mercurial 'convert_revision' extra to a Bazaar 'converted-from' revprop.

    """
    try:
        (kind, revid) = rev.split(":", 1)
    except ValueError:
        return "unspecified %s\n" % rev
    if kind == "svn":
        url, revnum = revid.rsplit('@', 1)
        revnum = int(revnum)
        parts = url.split('/', 1)
        uuid = parts.pop(0)
        mod = ''
        if parts:
            mod = parts[0]
        return "svn %s:%d:%s\n" % (uuid, revnum, urllib.quote(mod))
    else:
        raise KeyError("Unknown VCS '%s'" % kind)


def generate_convert_revision(line):
    """Generate 'convert_revision'.

    """
    (kind, revid) = line.split(" ", 1)
    if kind == "svn":
        (uuid, revnumstr, branchpathstr) = revid.split(":", 2)
        revnum = int(revnumstr)
        branchpath = urllib.unquote(branchpathstr)
        return "svn:%s/%s@%s" % (uuid, branchpath, revnum)
    elif kind == "unspecified":
        return revid
    else:
        raise KeyError("Unknown VCS '%s'" % kind)


def flags_kind(flags, path):
    """Determine the Bazaar file kind from the Mercurial flags for a path.

    :param flags: Mercurial flags dictionary
    :param path: Path
    :return: kind (either 'file' or 'symlink')
    """
    if 'l' in flags.get(path, ""):
        return 'symlink'
    return 'file'


def as_hg_parents(parents, lookup_revid):
    """Convert a list of Bazaar parent revision ids to Mercurial parents.

    :param parents: Iterable over the Bazaar parent revision ids
    :param lookup_revid: Callback converting a revid to a Mercurial id
    :return: 2-tuple with Mercurial parents
    """
    ret = []
    for p in parents[:2]:
        try:
            ret.append(lookup_revid(p))
        except KeyError:
            ret.append(mercurial.node.nullid)
    while len(ret) < 2:
        ret.append(mercurial.node.nullid)
    return tuple(ret)


def as_bzr_parents(parents, lookup_id):
    """Convert a 2-tuple with Mercurial parents to a list of Bazaar parents.

    :param parents: 2-tuple with Mercurial parents
    :param lookup_id: Callback for looking up a revision id by Mercurial id
    :return: List of revision ids
    """
    assert len(parents) == 2
    if parents[0] == mercurial.node.nullid:
        if parents[1] == mercurial.node.nullid:
            return ()
        else:
            return (_mod_revision.NULL_REVISION, lookup_id(parents[1]))
    else:
        ret = [lookup_id(parents[0])]
        if parents[1] != mercurial.node.nullid:
            ret.append(lookup_id(parents[1]))
        return tuple(ret)


def files_from_delta(delta, tree, revid):
    """Create a Mercurial-style 'files' set from a Bazaar tree delta.

    :param delta: breezy.delta.TreeDelta instance
    :param tree: Tree
    :param revid: Revision id
    :return: Set with changed files
    """
    ret = set()
    for change in delta.added + delta.removed + delta.modified:
        (path, id, kind) = change[:3]
        if kind not in ('file', 'symlink'):
            continue
        if not tree.has_id(id) or tree.get_file_revision(id) == revid:
            ret.add(path)
    for (path, id, old_kind, new_kind) in delta.kind_changed:
        if old_kind in ('file', 'symlink') or new_kind in ('file', 'symlink'):
            ret.add(path)
    for (oldpath, newpath, id, kind, text_modified, meta_modified) in delta.renamed:
        if kind in ('file', 'symlink'):
            ret.update([oldpath, newpath])
    return sorted([p.encode("utf-8") for p in ret])


def entry_sha1(entry):
    """Calculate the full text sha1 for an inventory entry.

    :param entry: Inventory entry
    :return: SHA1 hex string
    """
    if entry.kind == 'symlink':
        return osutils.sha_string(entry.symlink_target)
    else:
        return entry.text_sha1


def find_matching_entry(parent_trees, path, text_sha1):
    for i, ptree in enumerate(parent_trees):
        fid = ptree.path2id(path)
        if fid is None:
            continue
        if entry_sha1(ptree.inventory[fid]) == text_sha1:
            return i
    return None


def manifest_and_flags_from_tree(parent_trees, tree, mapping, parent_node_lookup):
    """Generate a manifest from a Bazaar tree.

    :param parent_trees: Parent trees
    :param tree: Tree
    :param mapping: Bzr<->Hg mapping
    :param parent_node_lookup: 2-tuple with functions to look up the nodes
        of paths in the tree's parents
    """
    assert len(parent_node_lookup) == 2
    unusual_fileids = {}
    def get_text_parents(path):
        assert type(path) == str
        ret = []
        for lookup in parent_node_lookup:
            try:
                ret.append(lookup(path))
            except KeyError:
                ret.append(mercurial.node.nullid)
        assert len(ret) == 2
        return tuple(ret)
    manifest = {}
    flags = {}
    for path, entry in tree.iter_entries_by_dir():
        this_sha1 = entry_sha1(entry)
        prev_entry = find_matching_entry(parent_trees, path, this_sha1)
        utf8_path = path.encode("utf-8")
        if entry.kind == 'symlink':
            flags[utf8_path] = 'l'
            if prev_entry is None:
                manifest[utf8_path] = hghash(entry.symlink_target, *get_text_parents(utf8_path))
        elif entry.kind == 'file':
            if entry.executable:
                flags[utf8_path] = 'x'
            if prev_entry is None:
                manifest[utf8_path] = hghash(tree.get_file_text(entry.file_id), *get_text_parents(utf8_path))
        if entry.kind in ('file', 'symlink') and prev_entry is not None:
            manifest[utf8_path] = parent_node_lookup[prev_entry](utf8_path)
        if ((mapping.generate_file_id(utf8_path) != entry.file_id or entry.kind == 'directory') and
            (parent_trees == [] or parent_trees[0].path2id(path) != entry.file_id)):
            unusual_fileids[utf8_path] = entry.file_id
    return (manifest, flags, unusual_fileids)


def escape_path(path):
    """Escape a path for use as a file id.

    :param path: path to escape
    :return: file id
    """
    return path.replace('_', '__').replace('/', '_s').replace(' ', '_w')


def unescape_path(file_id):
    """Unescape a file id created with escape_path().

    :param file_id: File id to unescape
    :return: Unescaped path
    """
    ret = []
    i = 0
    while i < len(file_id):
        if file_id[i] != '_':
            ret.append(file_id[i])
        else:
            if file_id[i+1] == '_':
                ret.append("_")
            elif file_id[i+1] == 's':
                ret.append("/")
            elif file_id[i+1] == 'w':
                ret.append(" ")
            else:
                raise ValueError("unknown escape character %s" % file_id[i+1])
            i += 1
        i += 1
    return "".join(ret)


class HgMappingv1(foreign.VcsMapping):
    """Class that maps between Bazaar and Mercurial semantics."""
    experimental = False
    revid_prefix = "hg-v1"

    def __init__(self):
        super(HgMappingv1, self).__init__(foreign_hg)

    def __str__(self):
        return self.revid_prefix

    @classmethod
    def revision_id_foreign_to_bzr(cls, revision_id):
        """See VcsMapping.revision_id_foreign_to_bzr."""
        if revision_id == mercurial.node.nullid:
            return _mod_revision.NULL_REVISION
        if len(revision_id) == 20:
            hexhgrevid = hex(revision_id)
        elif len(revision_id) == 40:
            hexhgrevid = revision_id
        else:
            raise AssertionError("Invalid hg id %r" % revision_id)
        return "%s:%s" % (cls.revid_prefix, hexhgrevid)

    @classmethod
    def revision_id_bzr_to_foreign(cls, revision_id):
        """See VcsMapping.revision_id_foreign_to_bzr."""
        if revision_id == _mod_revision.NULL_REVISION:
            return mercurial.node.nullid, cls()
        if not revision_id.startswith("%s:" % cls.revid_prefix):
            raise errors.InvalidRevisionId(revision_id, cls)
        return bin(revision_id[len(cls.revid_prefix)+1:]), cls()

    @classmethod
    def generate_file_id(self, path):
        """Create a synthetic file_id for an hg file."""
        if isinstance(path, unicode):
            path = path.encode("utf-8")
        if path == "":
            return "TREE_ROOT"
        return "hg:" + escape_path(path)

    @classmethod
    def parse_file_id(self, fileid):
        """Parse a file id."""
        assert isinstance(fileid, str)
        if not fileid.startswith("hg:"):
            raise ValueError
        return unescape_path(fileid[len("hg:"):])

    def export_revision(self, rev, lossy=True, fileids={}):
        user = rev.committer
        time = rev.timestamp
        timezone = -rev.timezone
        extra = {}
        manifest = None
        for name, value in rev.properties.iteritems():
            if name == 'manifest':
                manifest = mercurial.node.bin(value)
            elif name.startswith("hg:extra:"):
                extra[name[len("hg:extra:"):]] = base64.b64decode(value)
            elif name == 'rebase-of':
                try:
                    hgid, mapping = self.revision_id_bzr_to_foreign(value)
                except errors.InvalidRevisionId:
                    extra["bzr-revprop-"+name] = value.encode("utf-8")
                else:
                    assert len(hgid) == 20
                    extra['rebase_source'] = mercurial.node.hex(hgid)
            elif name == 'converted-from':
                if value.count('\n') <= 1:
                    continue
                extra['convert_revision'] = generate_convert_revision(value.splitlines()[-2])
            else:
                assert not ":" in name
                extra["bzr-revprop-"+name] = value.encode("utf-8")
        if not lossy and not rev.revision_id.startswith(self.revid_prefix + ":"):
            extra["bzr-mapping"] = str(self)
            extra["bzr-revision-id"] = rev.revision_id
            if len(rev.parent_ids) > 2:
                extra["bzr-extra-parents"] = " ".join(rev.parent_ids[2:])
            if fileids:
                extra["bzr-fileids"] = bencode.bencode(sorted(fileids.items()))
        desc = rev.message
        return (manifest, user, (time, timezone), desc, extra)

    def import_revision(self, revid, parent_ids, hgrevid, manifest, user,
                        (time, timezone), desc, extra):
        result = foreign.ForeignRevision(hgrevid, self, revid)
        result.parent_ids = parent_ids
        if type(desc) != unicode:
            raise AssertionError
        result.message = desc
        result.inventory_sha1 = ""
        result.timezone = -timezone
        result.timestamp = time
        if type(user) != unicode:
            raise AssertionError
        result.committer = user
        result.properties = {
                'manifest': mercurial.node.hex(manifest)
                }
        fileids = {}
        for name, value in extra.iteritems():
            if name.startswith("bzr-revprop-"):
                result.properties[name[len("bzr-revprop-")]] = value.decode("utf-8")
            elif name == "bzr-extra-parents":
                result.parent_ids += tuple(value.split(" "))
            elif name == "bzr-revision-id":
                result.revision_id = value
            elif name == "bzr-fileids":
                fileids = dict(bencode.bdecode(value))
            elif name == "convert_revision":
                result.properties['converted-from'] = convert_converted_from(value)
            elif name == "rebase_source":
                result.properties['rebase-of'] = self.revision_id_foreign_to_bzr(value)
            elif name.startswith("bzr-"):
                trace.mutter("unknown bzr extra %s: %r", name, value)
            else:
                result.properties["hg:extra:" + name] = base64.b64encode(value)
        if len(hgrevid) == 40:
            hghexrevid = hgrevid
        else:
            hghexrevid = mercurial.node.hex(hgrevid)
        result.properties['converted-from'] = \
                result.properties.get('converted-from', '') + \
                "hg %s\n" % hghexrevid
        return result, fileids


class ExperimentalHgMapping(HgMappingv1):

    experimental = True
    revid_prefix = "hg-experimental"


class HgMappingRegistry(foreign.VcsMappingRegistry):
    """Registry of all Bazaar <-> Mercurial mappings."""

    def revision_id_bzr_to_foreign(self, bzr_revid):
        if bzr_revid == _mod_revision.NULL_REVISION:
            return mercurial.node.nullid, None
        if not bzr_revid.startswith("hg-"):
            raise errors.InvalidRevisionId(bzr_revid, None)
        (mapping_version, hg_ref) = bzr_revid.split(":", 1)
        mapping = self.get(mapping_version)
        return mapping.revision_id_bzr_to_foreign(bzr_revid)

    parse_revision_id = revision_id_bzr_to_foreign


mapping_registry = HgMappingRegistry()
mapping_registry.register_lazy("hg-v1", "breezy.plugins.hg.mapping",
    "HgMappingv1")
mapping_registry.register_lazy("hg-experimental", "breezy.plugins.hg.mapping",
    "ExperimentalHgMapping")
mapping_registry.set_default('hg-v1')

class ForeignHg(foreign.ForeignVcs):
    """Foreign Mercurial."""

    @property
    def branch_format(self):
        from breezy.plugins.hg.branch import HgBranchFormat
        return HgBranchFormat()

    @property
    def repository_format(self):
        from breezy.plugins.hg.repository import HgRepositoryFormat
        return HgRepositoryFormat()

    def __init__(self):
        super(ForeignHg, self).__init__(mapping_registry)
        self.abbreviation = "hg"

    @classmethod
    def show_foreign_revid(cls, foreign_revid):
        """See ForeignVcs.show_foreign_revid."""
        return { "hg commit": hex(foreign_revid) }

    def serialize_foreign_revid(self, foreign_revid):
        return mercurial.node.hex(foreign_revid)


foreign_hg = ForeignHg()
default_mapping = mapping_registry.get_default()()
