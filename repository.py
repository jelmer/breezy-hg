# Copyright (C) 2005, 2006 Canonical Ltd
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

import bzrlib.repository
from bzrlib.revision import NULL_REVISION

from bzrlib.plugins.hg.foreign import (
    versionedfiles,
    )

class HgRepositoryFormat(bzrlib.repository.RepositoryFormat):
    """Mercurial Repository Format.

    This is currently not aware of different repository formats,
    but simply relies on the installed copy of mercurial to 
    support the repository format.
    """

    def get_format_description(self):
        """See RepositoryFormat.get_format_description()."""
        return "Mercurial Repository"


class HgRepository(bzrlib.repository.Repository):
    """An adapter to mercurial repositories for bzr."""

    def __init__(self, hgrepo, hgdir, lockfiles):
        self._hgrepo = hgrepo
        self.bzrdir = hgdir
        self.control_files = lockfiles
        self._format = HgRepositoryFormat()
        self.base = hgdir.root_transport.base
        self._fallback_repositories = []
        self.texts = None
        self.signatures = versionedfiles.VirtualSignatureTexts(self)
        self.revisions = versionedfiles.VirtualRevisionTexts(self)

    def get_parent_map(self, revids):
        ret = {}
        for revid in revids:
            if revid == NULL_REVISION:
                ret[revid] = ()
            else:
                hg_ref, mapping = mapping_registry.revision_id_bzr_to_foreign(revid)
                ret[revid] = tuple([mapping.revision_id_foreign_to_bzr(r) for r in self._hgrepo.changelog.parents(hg_ref)])
        return ret

    def _check(self, revision_ids):
        # TODO: Call out to mercurial for consistency checking?
        return bzrlib.branch.BranchCheckResult(self)

    def get_mapping(self):
        return default_mapping # for now

    def get_inventory(self, revision_id):
        """Synthesize a bzr inventory from an hg manifest...

        how this works:
        we grab the manifest for revision_id
        we create an Inventory
        for each file in the manifest we:
            * if the dirname of the file is not in the inventory, we add it
              recursively, with an id of the path with / replaced by :, and a 
              prefix of 'hg:'. The directory gets a last-modified value of the
              topologically oldest file.revision value under it in the 
              inventory. In the event of multiple revisions with no topological
              winner - that is where there is more than one root, alpha-sorting
              is used as a tie-break.
            * use the files revlog to get the 'linkrev' of the file which 
              takes us to the revision id that introduced that revision. That
              revision becomes the revision_id in the inventory
            * check for executable status in the manifest flags
            * add an entry for the file, of type file, executable if needed,
              and an id of 'hg:path' with / replaced by :.
        """
        # TODO: this deserves either _ methods on HgRepository, or a method
        # object. Its too big!
        hgid = self.get_mapping().revision_id_foreign_to_bzr(revision_id)
        log = self._hgrepo.changelog.read(hgid)
        manifest = self._hgrepo.manifest.read(log[0])
        all_relevant_revisions = self.get_revision_graph(revision_id)
        ancestry_cache = {}
        result = Inventory()
        # each directory is a key - i.e. 'foo'
        # the value is the current chosen revision value for it.
        # we walk up the hierarchy - when a dir changes .revision, its parent
        # must also change if the new value is older than the parents one.
        directories = {}
        def get_ancestry(some_revision_id):
            try:
                return ancestry_cache[some_revision_id]
            except KeyError:
                pass
            ancestry = set()
            # add what can be reached from some_revision_id
            # TODO: must factor this trivial iteration in bzrlib.graph cleanly.
            pending = set([some_revision_id])
            while len(pending) > 0:
                node = pending.pop()
                ancestry.add(node)
                for parent_id in all_relevant_revisions[node]:
                    if parent_id not in ancestry:
                        pending.add(parent_id)
            ancestry_cache[some_revision_id] = ancestry
            return ancestry
        def path_id(path):
            """Create a synthetic file_id for an hg file."""
            return "hg:" + path.replace('/', ':')
        def pick_best_creator_revision(revision_a, revision_b):
            """Picks the best creator revision from a and b.

            If a is an ancestor of b, a wins, and vice verca.
            If neither is an ancestor of the other, the lowest value wins.
            """
            # TODO make this much faster - use a local cache of the ancestry
            # sets.
            if revision_a in get_ancestry(revision_b):
                return revision_a
            elif revision_b in get_ancestry(revision_a):
                return revision_b
            elif revision_a < revision_b:
                return revision_a
            else:
                return revision_b
        def add_dir_for(file, file_revision_id):
            """ensure that file can be added by adding its parents.

            this is horribly inefficient at the moment, proof of concept.

            This is called for every path under each dir, and will update the
            .revision for it older each time as the file age is determined.
            """
            path = os.path.dirname(file)
            if path == '':
                # special case the root node.
                return
            if result.has_filename(path):
                # check for a new revision
                current_best = directories[path]
                new_best = pick_best_creator_revision(current_best, file_revision_id)
                if new_best != current_best:
                    # new revision found, push this up
                    # XXX could hand in our result as a hint?
                    add_dir_for(path, file_revision_id)
                    # and update our chosen one
                    directories[path] = file_revision_id
                return
            # the dir is not present. Add its parent too:
            add_dir_for(path, file_revision_id)
            # add the dir to the directory summary for creation detection
            directories[path] = file_revision_id
            # and put it in the inventory. The revision value is assigned later.
            entry = result.add_path(path, 'directory', file_id=path_id(path))
        # this can and should be tuned, but for now its just fine - its a 
        # proof of concept. add_path is part of the things to tune, as is
        # the dirname() calls.
        known_manifests = {}
        """manifests addressed by changelog."""
        for file, file_revision in manifest.items():
            revlog = self._hgrepo.file(file)
            changelog_index = revlog.linkrev(file_revision)

            # find when the file was modified. 
            # start with the manifest nodeid
            current_log = log
            # we should find all the tails, and then when there are > 2 heads
            # record a new revision id at the join. We can detect this by
            # walking out from each head and assigning ids to them, when two
            # parents have the same manifest assign a new id.
            # TODO currently we just pick *a* tail.
            file_tails = []
            current_manifest = manifest
            # cls - changelogs
            parent_cls = set(self._hgrepo.changelog.parents(hgid))
            good_id = hgid
            done_cls = set()
            # walk the graph, any node at a time to find the last change point.
            while parent_cls:
                current_cl = parent_cls.pop()
                # the nullid isn't useful.
                if current_cl == mercurial.node.nullid:
                    continue
                if current_cl not in known_manifests:
                    current_manifest_id = self._hgrepo.changelog.read(current_cl)[0]
                    known_manifests[current_cl] = self._hgrepo.manifest.read(
                        current_manifest_id)
                current_manifest = known_manifests[current_cl]
                done_cls.add(current_cl)
                if current_manifest.get(file, None) != file_revision:
                    continue
                # unchanged in parent, advance to the parent.
                good_id = current_cl
                for parent_cl in self._hgrepo.changelog.parents(current_cl):
                    if parent_cl not in done_cls:
                        parent_cls.add(parent_cl)
            modified_revision = self.get_mapping().revision_id_foreign_to_bzr(good_id)
            # dont use the following, it doesn't give the right results consistently.
            # modified_revision = bzrrevid_from_hg(
            #     self._hgrepo.changelog.index[changelog_index][7])
            # now walk to find the introducing revision.
            parent_cl_ids = set([(None, hgid)])
            good_id = hgid
            done_cls = set()
            while parent_cl_ids:
                current_cl_id_child, current_cl_id = parent_cl_ids.pop()
                # the nullid isn't useful.
                if current_cl_id == mercurial.node.nullid:
                    continue
                if current_cl_id not in known_manifests:
                    current_manifest_id = self._hgrepo.changelog.read(current_cl_id)[0]
                    known_manifests[current_cl_id] = self._hgrepo.manifest.read(
                        current_manifest_id)
                current_manifest = known_manifests[current_cl_id]
                done_cls.add(current_cl_id)
                if current_manifest.get(file, None) is None:
                    # file is not in current manifest: its a tail, cut here.
                    good_id = current_cl_id_child
                    continue
                # walk to the parents
                if (mercurial.node.nullid, mercurial.node.nullid) == self._hgrepo.changelog.parents(current_cl_id):
                    # we have reached the root:
                    good_id = current_cl_id
                    continue
                for parent_cl in self._hgrepo.changelog.parents(current_cl_id):
                    if parent_cl not in done_cls:
                        parent_cl_ids.add((current_cl_id, parent_cl))
            introduced_at_path_revision = self.get_mapping().revision_id_foreign_to_bzr(good_id)
            add_dir_for(file, introduced_at_path_revision)
            entry = result.add_path(file, 'file', file_id=path_id(file))
            entry.text_size = revlog.size(revlog.nodemap[file_revision])
            # its a shame we need to pull the text out. is there a better way?
            # TODO: perhaps we should use readmeta here to figure out renames ?
            text = revlog.read(file_revision)
            entry.text_sha1 = sha_strings(text)
            if manifest.execf(file):
                entry.executable = True
            entry.revision = modified_revision
        for dir, dir_revision_id in directories.items():
            dirid = path_id(dir)
            result[dirid].revision = dir_revision_id
        return result

    def get_revision(self, revision_id):
        hgrevid, mapping = mapping_registry.revision_id_bzr_to_foreign(revision_id)
        result = ForeignRevision(hgrevid, None, revision_id)
        hgchange = self._hgrepo.changelog.read(hgrevid)
        hgparents = self._hgrepo.changelog.parents(hgrevid)
        result.parent_ids = []
        if hgparents[0] != mercurial.node.nullid:
            result.parent_ids.append(mapping.revision_id_foreign_to_bzr(hgparents[0]))
        if hgparents[1] != mercurial.node.nullid:
            result.parent_ids.append(mapping.revision_id_foreign_to_bzr(hgparents[1]))
        result.message = hgchange[4]
        result.inventory_sha1 = ""
        result.timezone = -hgchange[2][1]
        result.timestamp = hgchange[2][0]
        result.committer = hgchange[1]
        return result

    def get_revision_graph(self, revision_id=None):
        if revision_id is None:
            raise NotImplementedError("get_revision_graph with no parents not implemented yet.")
        else:
            # add what can be reached from revision_id
            result = {}
            pending = set([revision_id])
            while len(pending) > 0:
                node = pending.pop()
                result[node] = self.get_revision(node).parent_ids
                for revision_id in result[node]:
                    if revision_id not in result:
                        pending.add(revision_id)
            return result
    
    def has_revision(self, revision_id):
        return mapping_registry.revision_id_bzr_to_foreign(revision_id)[0] in self._hgrepo.changelog.nodemap

    def is_shared(self):
        """Whether this repository is being shared between multiple branches. 
        
        Always False for Mercurial for now.
        """
        return False



