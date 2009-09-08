# Copyright (C) 2005, 2006 Canonical Ltd
# Copyright (C) 2008-2009 Jelmer Vernooij <jelmer@samba.org>
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

# InterHgRepository.findmissing is based on 
# mercurial.localrepo.localrepository.findcommonincoming:
#
# Copyright 2005-2007 Matt Mackall <mpm@selenic.com>
# Published under the GNU GPLv2 or later

"""Inter-repository operations involving Mercurial repositories."""

import mercurial.node

from bzrlib import (
    errors,
    trace,
    ui,
    )
from bzrlib.decorators import (
    needs_write_lock,
    )
from bzrlib.repository import (
    InterRepository,
    )
from bzrlib.tsort import (
    topo_sort,
    )
from bzrlib.versionedfile import (
    ChunkedContentFactory,
    FulltextContentFactory,
    )

from bzrlib.plugins.hg.mapping import (
    mapping_registry,
    )

class FromHgRepository(InterRepository):
    """Hg to any repository actions."""

    @classmethod
    def _get_repo_format_to_test(self):
        """The format to test with - as yet there is no HgRepoFormat."""
        return None

    def _unpack_chunk_iter(self, chunk):
        raise NotImplementedError(self._unpack_chunk_iter)

    def addchangegroup(self, cg, mapping):
        """Import a Mercurial changegroup into the target repository."""
        # Changeset
        chunkiter = mercurial.changegroup.chunkiter(cg)
        # FIXME: read changesets chunks, insert
        self._unpack_chunk_iter(chunkiter)
        # Manifest
        chunkiter = mercurial.changegroup.chunkiter(cg)
        # FIXME: read manifest chunks, insert
        self._unpack_chunk_iter(chunkiter)
        # Texts
        while 1:
            f = mercurial.changegroup.getchunk(cg)
            if not f:
                break
            fileid = mapping.generate_file_id(f)
            chunkiter = mercurial.changegroup.chunkiter(cg)
            self.target.texts.insert_record_stream(
                self._unpack_chunk_iter(chunkiter))

    def get_target_heads(self):
        """Determine the heads in the target repository."""
        # FIXME: This should be more efficient
        all_revs = self.target.all_revision_ids()
        parent_map = self.target.get_parent_map(all_revs)
        all_parents = set()
        map(all_parents.update, parent_map.itervalues())
        mapping = self.source.get_mapping()
        return set([mapping.revision_id_bzr_to_foreign(revid)[0] for revid in set(all_revs) - all_parents])

    def heads(self, fetch_spec, revision_id):
        """Determine the Mercurial heads to fetch."""
        if fetch_spec is not None:
            mapping = self.source.get_mapping()
            return [mapping.revision_id_bzr_to_foreign(head)[0] for head in fetch_spec.heads]
        if revision_id is not None:
            mapping = self.source.get_mapping()
            return [mapping.revision_id_bzr_to_foreign(revision_id)[0]]
        return self.source._hgrepo.heads()

    def has_hgid(self, id):
        if id == mercurial.node.nullid:
            return True
        if len(self.has_hgids([id])) == 1:
            return True
        return False

    def has_hgids(self, ids):
        """Check whether the specified Mercurial ids are present."""
        mapping = self.source.get_mapping()
        revids = set([mapping.revision_id_foreign_to_bzr(h) for h in ids])
        return set([
            mapping.revision_id_bzr_to_foreign(revid) 
            for revid in self.target.has_revisions(revids)])

    def findmissing(self, heads):
        """Find the set of ancestors of heads missing from target."""

        unknowns = set(heads) - self.has_hgids(heads)
        if not unknowns:
            return []
        seen = set()
        search = []
        fetch = set()
        seenbranch = set()
        remote = self.source._hgrepo
        req = set(unknowns)

        # search through remote branches
        # a 'branch' here is a linear segment of history, with four parts:
        # head, root, first parent, second parent
        # (a branch always has two parents (or none) by definition)
        unknowns = remote.branches(unknowns)
        while unknowns:
            r = []
            while unknowns:
                n = unknowns.pop(0)
                if n[0] in seen:
                    continue
                trace.mutter("examining %s:%s", mercurial.node.short(n[0]), mercurial.node.short(n[1]))
                if n[0] == mercurial.node.nullid: # found the end of the branch
                    pass
                elif n in seenbranch:
                    trace.mutter("branch already found")
                    continue
                elif n[1] and self.has_hgid(n[1]): # do we know the base?
                    trace.mutter("found incomplete branch %s:%s", 
                        mercurial.node.short(n[0]), mercurial.node.short(n[1]))
                    search.append(n[0:2]) # schedule branch range for scanning
                    seenbranch.add(n)
                else:
                    if n[1] not in seen and n[1] not in fetch:
                        if self.has_hgid(n[2]) and self.has_hgid(n[3]):
                            trace.mutter("found new changeset %s", mercurial.node.short(n[1]))
                            fetch.add(n[1]) # earliest unknowns
                    for p in n[2:4]:
                        if p not in req and not self.has_hgid(p):
                            r.append(p)
                            req.add(p)
                seen.add(n[0])

            if r:
                for p in xrange(0, len(r), 10):
                    for b in remote.branches(r[p:p+10]):
                        trace.mutter("received %s:%s", mercurial.node.short(b[0]), mercurial.node.short(b[1]))
                        unknowns.append(b)

        # do binary search on the branches we found
        while search:
            newsearch = []
            for n, l in zip(search, remote.between(search)):
                l.append(n[1])
                p = n[0]
                f = 1
                for i in l:
                    trace.mutter("narrowing %d:%d %s", f, len(l), mercurial.node.short(i))
                    if self.has_hgid(i):
                        if f <= 2:
                            trace.mutter("found new branch changeset %s", mercurial.node.short(p))
                            fetch.add(p)
                        else:
                            trace.mutter("narrowed branch search to %s:%s", 
                                          mercurial.node.short(p), mercurial.node.short(i))
                            newsearch.append((p, i))
                        break
                    p, f = i, f * 2
                search = newsearch
        return fetch

    @needs_write_lock
    def copy_content(self, revision_id=None, basis=None):
        """See InterRepository.copy_content. Partial implementation of that.

        To date the revision_id and basis parameters are not supported.
        """
        assert revision_id is None
        assert basis is None
        self.target.fetch(self.source)


class FromLocalHgRepository(FromHgRepository):
    """Local Hg repository to any repository actions."""

    def __init__(self, source, target):
        FromHgRepository.__init__(self, source, target)
        self._inventories = {}

    @needs_write_lock
    def fetch(self, revision_id=None, pb=None, find_ghosts=False, 
              fetch_spec=None):
        """Fetch revisions. This is a partial implementation."""
        # TODO: make this somewhat faster - calculate the specific needed
        # file versions and then pull all of those per file, followed by
        # inserting the inventories and revisions, rather than doing 
        # rev-at-a-time.
        needed = {}
        pending = set([mapping.revision_id_foreign_to_bzr(revid) for revid in self.heads(fetch_spec, revision_id)])
        # plan it.
        # we build a graph of the revisions we need, and a
        # full graph which we use for topo-sorting (we need a partial-
        # topo-sorter done to avoid this. TODO: a partial_topo_sort.)
        # so needed is the revisions we need, and needed_graph is the entire
        # graph, using 'local' sources for establishing the graph where
        # possible. TODO: also, use the bzr knit graph facility to seed the
        # graph once we encounter revisions we know about.
        needed_graph = {}
        while len(pending) > 0:
            node = pending.pop()
            try:
                rev = self.target.get_revision(node)
            except errors.NoSuchRevision:
                rev = self.source.get_revision(node)
                needed[node] = rev
            parent_ids = rev.parent_ids
            needed_graph[node] = parent_ids
            for revision_id in parent_ids:
                if revision_id not in needed_graph:
                    pending.add(revision_id)
        order = topo_sort(needed_graph.items())
        # order is now too aggressive: filter to just what we need:
        order = [rev_id for rev_id in order if rev_id in needed]
        self.target.start_write_group()
        try:
            self._fetch_hg_revs(order, needed)
        finally:
            self.target.commit_write_group()

    def _get_inventories(self, revision_ids):
        ret = []
        for revid in revision_ids:
            try:
                ret.append(self._inventories[revid])
            except KeyError:
                # if its not in the cache, its in target already
                self._inventories[revid] = self.target.get_inventory(revid)
                ret.append(self._inventories[revid])
        return ret

    def _fetch_hg_rev(self, revision):
        inventory = self.source.get_inventory(revision.revision_id)
        self._inventories[revision.revision_id] = inventory
        hgrevid, mapping = mapping_registry.revision_id_bzr_to_foreign(revision.revision_id)
        log = self.source._hgrepo.changelog.read(hgrevid)
        manifest = self.source._hgrepo.manifest.read(log[0])
        for fileid in inventory:
            #if fileid == bzrlib.inventory.ROOT_ID:
            #    continue
            entry = inventory[fileid]
            if inventory[fileid].revision == revision.revision_id:
                # changed in this revision
                entry = inventory[fileid]
                # changing the parents-to-insert-as algorithm here will
                # cause pulls from hg to change the per-file graph.
                # BEWARE of doing that.
                previous_inventories = self._get_inventories(
                    revision.parent_ids)
                file_heads = entry.parent_candidates(
                    previous_inventories)

                if entry.kind == 'directory':
                    # a bit of an abstraction variation, but we dont have a
                    # real tree for entry to read from, and it would be 
                    # mostly dead weight to have a stub tree here.
                    records = [
                        ChunkedContentFactory(
                            (fileid, revision.revision_id),
                            tuple([(fileid, revid) for revid in file_heads]),
                            None,
                            [])]
                else:
                    # extract text and insert it.
                    path = inventory.id2path(fileid)
                    revlog = self.source._hgrepo.file(path)
                    filerev = manifest[path]
                    # TODO: perhaps we should use readmeta here to figure out renames ?
                    text = revlog.read(filerev)
                    records = [
                            FulltextContentFactory(
                                (fileid, revision.revision_id),
                                tuple([(fileid, revid) for revid in file_heads]),
                                None, text)]
                self.target.texts.insert_record_stream(records)
        inventory.revision_id = revision.revision_id
        inventory.root.revision = revision.revision_id # Yuck. FIXME
        self.target.add_inventory(revision.revision_id, inventory, 
                                  revision.parent_ids)
        self.target.add_revision(revision.revision_id, revision)

    def _fetch_hg_revs(self, order, revisions):
        total = len(order)
        pb = ui.ui_factory.nested_progress_bar()
        try:
            for index, revision_id in enumerate(order):
                pb.update('fetching revisions', index, total)
                self._fetch_hg_rev(revisions[revision_id])
        finally:
            pb.finished()
        return total, 0

    @staticmethod
    def is_compatible(source, target):
        """Be compatible with HgLocalRepositories."""
        from bzrlib.plugins.hg.repository import HgLocalRepository, HgRepository
        return (isinstance(source, HgLocalRepository) and 
                not isinstance(target, HgRepository))


class FromRemoteHgRepository(FromHgRepository):
    """Remote Hg repository to any repository actions."""

    @needs_write_lock
    def fetch(self, revision_id=None, pb=None, find_ghosts=False, 
              fetch_spec=None):
        """Fetch revisions. This is a partial implementation."""
        heads = self.heads(fetch_spec, revision_id)
        missing = self.findmissing(heads)
        if not missing:
            return
        cg = self.source._hgrepo.changegroup(missing, 'pull')
        mapping = self.source.get_mapping()
        self.addchangegroup(cg, mapping)

    @staticmethod
    def is_compatible(source, target):
        """Be compatible with HgRemoteRepositories."""
        from bzrlib.plugins.hg.repository import HgRemoteRepository, HgRepository
        return (isinstance(source, HgRemoteRepository) and
                not isinstance(target, HgRepository))


class InterHgRepository(FromHgRepository):

    @needs_write_lock
    def fetch(self, revision_id=None, pb=None, find_ghosts=False, 
              fetch_spec=None):
        """Fetch revisions. This is a partial implementation."""
        if revision_id is not None:
            raise NotImplementedError("revision_id argument not yet supported")
        if fetch_spec is not None:
            raise NotImplementedError("fetch_spec argument not yet supported")
        if self.target._hgrepo.local():
            self.target._hgrepo.pull(self.source._hgrepo)
        else:
            self.source._hgrepo.push(self.target._hgrepo)

    @staticmethod
    def is_compatible(source, target):
        """Be compatible with HgRemoteRepositories."""
        from bzrlib.plugins.hg.repository import HgRepository
        return (isinstance(source, HgRepository) and 
                isinstance(target, HgRepository))
