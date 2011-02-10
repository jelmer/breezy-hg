# Copyright (C) 2005, 2006, 2011 Canonical Ltd
# Copyright (C) 2008-2011 Jelmer Vernooij <jelmer@samba.org>
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

"""Mercurial Branch support."""

import os

from bzrlib import (
    errors,
    )
from bzrlib.branch import (
    BranchCheckResult,
    BranchFormat,
    BranchPushResult,
    GenericInterBranch,
    InterBranch,
    PullResult,
    )
from bzrlib.decorators import (
    needs_read_lock,
    )
from bzrlib.foreign import (
    ForeignBranch,
    )
from bzrlib.repository import (
    InterRepository,
    )
from bzrlib.tag import (
    BasicTags,
    )

from bzrlib.plugins.hg.changegroup import (
    dchangegroup,
    )

class NoPushSupport(errors.BzrError):
    _fmt = "Push is not yet supported for bzr-hg. Try dpush instead."


class HgTags(BasicTags):

    def __init__(self, branch):
        self.branch = branch

    def _get_hg_tags(self):
        raise NotImplementedError(self._get_hg_tags)

    def get_tag_dict(self):
        ret = {}
        hgtags = self._get_hg_tags()
        for name, value in hgtags.iteritems():
            ret[name] = self.branch.repository.lookup_foreign_revision_id(value)
        return ret

    def set_tag(self, name, value):
        self.branch.repository._hgrepo.tag([name],
            self.branch.repository.lookup_bzr_revision_id(value)[0],
            "Create tag %s" % name,
            True,
            self.branch.get_config().username(), None)


class LocalHgTags(HgTags):

    def _get_hg_tags(self):
        return self.branch.repository._hgrepo.tags()


class FileHgTags(HgTags):

    def __init__(self, branch, revid):
        self.branch = branch
        self.revid = revid

    def _get_hg_tags(self):
        revtree = self.branch.repository.revision_tree(self.revid)
        f = revtree.get_file_text(revtree.path2id(".hgtags"), ".hgtags")
        for l in f.readlines():
            (name, hgtag) = l.strip().split(" ")
            yield name, hgtag


class HgBranchFormat(BranchFormat):
    """Mercurial Branch Format.

    This is currently not aware of different branch formats,
    but simply relies on the installed copy of mercurial to
    support the branch format.
    """

    def get_format_description(self):
        """See BranchFormat.get_format_description()."""
        return "Mercurial Branch"

    def network_name(self):
        return "hg"

    def get_foreign_tests_branch_factory(self):
        from bzrlib.plugins.hg.tests.test_branch import ForeignTestsBranchFactory
        return ForeignTestsBranchFactory()


class LocalHgBranchFormat(HgBranchFormat):

    def supports_tags(self):
        """True if this format supports tags stored in the branch"""
        return True

    def make_tags(self, branch):
        """See bzrlib.branch.BranchFormat.make_tags()."""
        return LocalHgTags(branch)


class RemoteHgBranchFormat(HgBranchFormat):

    def supports_tags(self):
        """True if this format supports tags stored in the branch"""
        return False

    def make_tags(self, branch):
        """See bzrlib.branch.BranchFormat.make_tags()."""
        raise NotImplementedError


class HgBranchConfig(object):
    """Access Branch Configuration data for an HgBranch.

    This is not fully compatible with bzr yet - but it should be made so.
    """

    def __init__(self, branch):
        self._branch = branch
        self._ui = branch.repository._hgrepo.ui

    def username(self):
        return self._ui.config("username", "default")

    def get_nickname(self):
        # remove the trailing / and take the basename.
        return os.path.basename(self._branch.base[:-1])

    def get_parent(self):
        return self._ui.config("paths", "default")

    def set_parent(self, url):
        self._ui.setconfig("paths", "default", url)

    def has_explicit_nickname(self):
        return False

    def get_user_option(self, name):
        return None

    def get_user_option_as_bool(self, name):
        return False

    def set_user_option(self, name, value, warn_masked=False):
        pass # FIXME: Uhm?

    def log_format(self):
        """What log format should be used"""
        return "long"


class HgReadLock(object):

    def __init__(self, unlock):
        self.unlock = unlock


class HgWriteLock(object):

    def __init__(self, unlock):
        self.unlock = unlock


class HgBranch(ForeignBranch):
    """An adapter to mercurial repositories for bzr Branch objects."""

    def __init__(self, hgrepo, hgdir, lockfiles):
        self.repository = hgdir.open_repository()
        ForeignBranch.__init__(self, self.repository.get_mapping())
        self._hgrepo = hgrepo
        self.bzrdir = hgdir
        self.control_files = lockfiles
        self.base = hgdir.root_transport.base

    def _check(self):
        # TODO: Call out to mercurial for consistency checking?
        return BranchCheckResult(self)

    def get_child_submit_format(self):
        """Return the preferred format of submissions to this branch."""
        ret = self.get_config().get_user_option("child_submit_format")
        if ret is not None:
            return ret
        return "hg"

    def get_parent(self):
        """Return the URL of the parent branch."""
        return self.get_config().get_parent()

    def get_physical_lock_status(self):
        return self.control_files.get_physical_lock_status()

    def get_push_location(self):
        """Return default push location of this branch."""
        # TODO: Obtain "repository default"
        return None

    def set_push_location(self, url):
        self.get_config().set_parent(url)

    def get_config(self):
        """See Branch.get_config().

        We return an HgBranchConfig, which is a stub class with little
        functionality.
        """
        return HgBranchConfig(self)

    def lock_write(self):
        self.control_files.lock_write()
        return HgWriteLock(self.unlock)

    @needs_read_lock
    def revision_history(self):
        revs = list(self.repository.iter_reverse_revision_history(self.last_revision()))
        revs.reverse()
        return revs

    def lock_read(self):
        self.control_files.lock_read()
        return HgReadLock(self.unlock)

    def is_locked(self):
        return self.control_files.is_locked()

    def unlock(self):
        self.control_files.unlock()

    def get_stacked_on_url(self):
        raise errors.UnstackableBranchFormat(self._format, self.base)

    def _set_parent_location(self, parent_url):
        self.get_config().set_parent(parent_url)

    def _synchronize_history(self, destination, revision_id):
        source_revision_id = self.last_revision()
        if revision_id is None:
            revision_id = source_revision_id
        destination.generate_revision_history(revision_id)


class HgLocalBranch(HgBranch):

    def __init__(self, hgrepo, hgdir, lockfiles):
        self._format = LocalHgBranchFormat()
        super(HgLocalBranch, self).__init__(hgrepo, hgdir, lockfiles)

    @needs_read_lock
    def last_revision(self):
        tip = self._hgrepo.lookup("tip")
        return self.repository.lookup_foreign_revision_id(tip,
            mapping=self.mapping)


class HgRemoteBranch(HgBranch):

    def __init__(self, hgrepo, hgdir, lockfiles):
        self._format = RemoteHgBranchFormat()
        super(HgRemoteBranch, self).__init__(hgrepo, hgdir, lockfiles)

    def supports_tags(self):
        return getattr(self.repository._hgrepo, "tags", None) is not None

    @needs_read_lock
    def last_revision(self):
        tip = self._hgrepo.lookup("tip")
        return self.mapping.revision_id_foreign_to_bzr(tip)


class InterHgBranch(GenericInterBranch):
    """InterBranch for two native Mercurial branches."""

    @staticmethod
    def _get_branch_formats_to_test():
        return []

    @staticmethod
    def is_compatible(source, target):
        """See InterBranch.is_compatible()."""
        return (isinstance(source, HgBranch) and isinstance(target, HgBranch))

    def pull(self, overwrite=False, stop_revision=None,
             possible_transports=None, local=False):
        """See InterBranch.pull()."""
        result = PullResult()
        result.source_branch = self.source
        result.target_branch = self.target
        result.old_revno, result.old_revid = self.target.last_revision_info()
        inter = InterRepository.get(self.source.repository,
                                    self.target.repository)
        if stop_revision is None:
            stop_revision = self.source.last_revision()
        inter.fetch(revision_id=stop_revision)
        if overwrite:
            req_base = None
        else:
            req_base = self.target.last_revision()
        self.target.generate_revision_history(stop_revision,
            req_base, self.source)
        result.new_revno, result.new_revid = self.target.last_revision_info()
        return result

    def push(self, overwrite=False, stop_revision=None):
        """See InterBranch.push()."""
        result = BranchPushResult()
        result.source_branch = self.source
        result.target_branch = self.target
        result.old_revno, result.old_revid = self.target.last_revision_info()
        inter = InterRepository.get(self.source.repository,
                                    self.target.repository)
        inter.fetch(revision_id=stop_revision)
        result.new_revno, result.new_revid = self.target.last_revision_info()
        return result


InterBranch.register_optimiser(InterHgBranch)


class FromHgBranch(GenericInterBranch):
    """InterBranch pulling from a Mercurial branch."""

    @staticmethod
    def _get_branch_formats_to_test():
        return []

    @staticmethod
    def is_compatible(source, target):
        """See InterBranch.is_compatible()."""
        return (isinstance(source, HgBranch) and
                not isinstance(target, HgBranch))

    def pull(self, overwrite=False, stop_revision=None,
             possible_transports=None, local=False):
        """See InterBranch.pull()."""
        result = PullResult()
        result.source_branch = self.source
        result.target_branch = self.target
        result.old_revno, result.old_revid = self.target.last_revision_info()
        inter = InterRepository.get(self.source.repository,
                                    self.target.repository)
        inter.fetch(revision_id=stop_revision)
        if overwrite:
            req_base = None
        else:
            req_base = self.target.last_revision()
        self.target.generate_revision_history(self.source.last_revision(),
            req_base, self.source)
        result.new_revno, result.new_revid = self.target.last_revision_info()
        tags = FileHgTags(self.target, result.new_revid)
        result.tag_conflicts = tags.merge_to(self.target.tags, overwrite)
        return result

    def push(self, overwrite=False, stop_revision=None):
        """See InterBranch.push()."""
        result = BranchPushResult()
        result.source_branch = self.source
        result.target_branch = self.target
        result.old_revid = self.target.last_revision()
        inter = InterRepository.get(self.source.repository,
                                    self.target.repository)
        if stop_revision is not None:
            stop_revision = self.source.last_revision()
        inter.fetch(revision_id=stop_revision)
        if overwrite:
            req_base = None
        else:
            req_base = self.target.last_revision()
        self.target.generate_revision_history(stop_revision, req_base,
            self.source)
        result.new_revid = self.target.last_revision()
        tags = FileHgTags(self.target, result.new_revid)
        result.tag_conflicts = tags.merge_to(self.target.tags, overwrite)
        return result


class HgBranchPushResult(BranchPushResult):

    def _lookup_revno(self, revid):
        assert isinstance(revid, str), "was %r" % revid
        # Try in source branch first, it'll be faster
        try:
            return self.source_branch.revision_id_to_revno(revid)
        except errors.NoSuchRevision:
            # FIXME: Check using graph.find_distance_to_null() ?
            return self.target_branch.revision_id_to_revno(revid)

    @property
    def old_revno(self):
        return self._lookup_revno(self.old_revid)

    @property
    def new_revno(self):
        return self._lookup_revno(self.new_revid)


class ToHgBranch(InterBranch):
    """InterBranch implementation that pushes into Hg."""

    @staticmethod
    def _get_branch_formats_to_test():
        return []

    @classmethod
    def is_compatible(self, source, target):
        return (not isinstance(source, HgBranch) and
                isinstance(target, HgBranch))

    def _push_helper(self, stop_revision=None, overwrite=False,
            lossy=False):
        graph = self.source.repository.get_graph()
        if stop_revision is None:
            stop_revision = self.source.last_revision()
        revs = graph.find_difference(self.target.last_revision(),
                                     stop_revision)[1]
        cg, revidmap = dchangegroup(self.source.repository,
                                    self.target.mapping, revs, lossy=lossy)
        heads = [revidmap[stop_revision]]
        remote = self.target.repository._hgrepo
        if remote.capable('unbundle'):
            remote.unbundle(cg, heads, None)
        else:
            remote.addchangegroup(cg, 'push', self.source.base)
            # TODO: Set heads
        if lossy:
            return dict((k, self.target.mapping.revision_id_foreign_to_bzr(v)) for (k, v) in revidmap.iteritems())

    @needs_read_lock
    def push(self, overwrite=True, stop_revision=None,
             _override_hook_source_branch=None):
        result = HgBranchPushResult()
        result.source_branch = self.source
        result.target_branch = self.target
        result.old_revid = self.target.last_revision()
        if stop_revision is None:
            stop_revision = self.source.last_revision()
        self._push_helper(stop_revision=stop_revision, overwrite=overwrite,
            lossy=False)
        # FIXME: Check for diverged branches
        result.new_revid = stop_revision
        return result

    @needs_read_lock
    def lossy_push(self, stop_revision=None):
        result = HgBranchPushResult()
        result.source_branch = self.source
        result.target_branch = self.target
        result.old_revid = self.target.last_revision()
        if stop_revision is None:
            stop_revision = self.source.last_revision()
        if stop_revision != result.old_revid:
            revidmap = self._push_helper(stop_revision=stop_revision,
                lossy=True)
            result.new_revid = revidmap.get(stop_revision, result.old_revid)
        else:
            result.new_revid = result.old_revid
        # FIXME: Check for diverged branches
        result.revidmap = revidmap
        return result


InterBranch.register_optimiser(FromHgBranch)
InterBranch.register_optimiser(ToHgBranch)
