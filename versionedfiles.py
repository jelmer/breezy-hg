# Copyright (C) 2009 Jelmer Vernooij <jelmer@samba.org>
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


"""VersionedFiles implementation on top of a Mercurial repository."""


from collections import (
    defaultdict,
    )

from bzrlib import (
    graph as _mod_graph,
    )

from bzrlib.revision import (
    NULL_REVISION,
    )

from bzrlib.versionedfile import (
    FulltextContentFactory,
    VersionedFile,
    VersionedFiles,
    )

from mercurial.error import (
    LookupError,
    )

from bzrlib.plugins.hg.mapping import (
    as_bzr_parents,
    )


class RevlogVersionedFile(VersionedFile):
    """Basic VersionedFile interface implementation that wraps a revlog."""

    def __init__(self, revlog):
        self._revlog = revlog

    def _lookup_id(self, key):
        return key

    def _reverse_lookup_id(self, key):
        return key

    def get_record_stream(self, nodes, order, include_delta_closure):
        for (key, ) in nodes:
            hgid = self._lookup_id(key)
            hgparents = self._revlog.parents(hgid)
            parents = tuple([(x,) for x in as_bzr_parents(hgparents, self._reverse_lookup_id)])
            yield FulltextContentFactory((key, ), parents, None, self._revlog.revision(hgid))

    def get_parent_map(self, revids):
        ret = {}
        for (revid, ) in revids:
            if revid == NULL_REVISION:
                ret[(revid,)] = ()
            else:
                hg_ref = self._lookup_id(revid)
                try:
                    ret[(revid, )] = tuple((x,) for x in as_bzr_parents(self._revlog.parents(hg_ref), self._reverse_lookup_id))
                except LookupError:
                    ret[(revid, )] = None
        return ret

    def keys(self):
        return list(self.iterkeys())

    def iterkeys(self):
        for x in xrange(len(self)):
            yield (self._reverse_lookup_id(self._revlog.node(x)), )

    def __len__(self):
        return len(self._revlog)

    def get_known_graph_ancestry(self, keys):
        """Get a KnownGraph instance with the ancestry of keys."""
        # most basic implementation is a loop around get_parent_map
        pending = set(keys)
        parent_map = {}
        while pending:
            this_parent_map = self.get_parent_map(pending)
            parent_map.update(this_parent_map)
            pending = set()
            map(pending.update, this_parent_map.itervalues())
            pending = pending.difference(parent_map)
        kg = _mod_graph.KnownGraph(parent_map)
        return kg



class ChangelogVersionedFile(RevlogVersionedFile):

    def __init__(self, revlog, repo):
        RevlogVersionedFile.__init__(self, revlog)
        self._repo = repo

    def _lookup_id(self, key):
        return self._repo.lookup_bzr_revision_id(key)[0]

    def _reverse_lookup_id(self, key):
        return self._repo.lookup_foreign_revision_id(key)


class ManifestVersionedFile(RevlogVersionedFile):

    def __init__(self, repo, revlog):
        RevlogVersionedFile.__init__(self, revlog)
        self.repo = repo

    def _lookup_id(self, key):
        clid = self.repo.lookup_bzr_revision_id(key)[0]
        return self.repo._hgrepo.changelog.read(clid)[0]

    def _reverse_lookup_id(self, key):
        return self.repo.lookup_foreign_revision_id(self._revlog.linkrev(key))


class RevlogVersionedFiles(VersionedFiles):
    """Basic VersionedFile interface implementation that wraps a revlog."""

    def __init__(self, repo, opener, mapping):
        self.repo = repo
        self._opener = opener
        self._mapping = mapping

    def _get_revlog(self, fileid):
        path = self._mapping.parse_file_id(fileid)
        return self._opener(path)

    def _get_manifest(self, revid):
        hgid, mapping = self.repo.lookup_bzr_revision_id(revid)
        manifest_id = self.repo._hgrepo.changelog.read(hgid)[0]
        return self.repo._hgrepo.manifest.read(manifest_id), mapping

    def get_record_stream(self, nodes, ordering, include_delta_closure):
        revisions = defaultdict(set)
        for (fileid, revision) in nodes:
            revisions[revision].add(fileid)
        needed = defaultdict(set)
        for revision, fileids in revisions.iteritems():
            manifest, mapping = self._get_manifest(revision)
            for fileid in fileids:
                path = mapping.parse_file_id(fileid)
                needed[fileid].add((manifest[path], revision))
        for fileid, nodes in needed.iteritems():
            revlog = self._get_revlog(fileid)
            vf = RevlogVersionedFile(revlog)
            for x in vf.get_record_stream([(node, ) for (node, revid) in nodes],
                    'unordered', include_delta_closure):
                x.key = (fileid, revid)
                if x.parents is not None:
                    x.parents = tuple([(fileid, y) for y in x.parents])
                yield x
