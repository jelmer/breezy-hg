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

# Please note that imports are delayed as much as possible here since
# if DWIM revspecs are supported this module is imported by __init__.py.

from breezy import version_info as breezy_version
from breezy.errors import (
    InvalidRevisionId,
    InvalidRevisionSpec,
    )
from breezy.revision import (
    NULL_REVISION,
    )
from breezy.revisionspec import (
    RevisionInfo,
    RevisionSpec,
    )

def valid_hg_csid(csid):
    import binascii
    try:
        binascii.unhexlify(csid)
    except TypeError:
        return False
    else:
        return True


class RevisionSpec_hg(RevisionSpec):
    """Selects a revision using a Mercurial revision."""

    help_txt = """Selects a revision using a Mercurial revision sha1.
    """

    prefix = 'hg:'
    wants_revision_history = False

    def _lookup_csid(self, branch, csid):
        from breezy.plugins.hg.repository import (
            MercurialSmartRemoteNotSupported,
            )
        mapping = getattr(branch, "mapping", None)
        if mapping is None:
            raise InvalidRevisionSpec(self.user_spec, branch)
        if mapping.vcs.abbreviation != "hg":
            raise InvalidRevisionSpec(self.user_spec, branch)
        bzr_revid = mapping.revision_id_foreign_to_bzr(csid)
        try:
            if branch.repository.has_revision(bzr_revid):
                if breezy_version < (2, 5):
                    history = branch.revision_history()
                    return RevisionInfo.from_revision_id(branch, bzr_revid, history)
                else:
                    return RevisionInfo.from_revision_id(branch, bzr_revid)
        except MercurialSmartRemoteNotSupported:
            return RevisionInfo(branch, None, bzr_revid)
        raise InvalidRevisionSpec(self.user_spec, branch)

    def _find_short_csid(self, branch, csid):
        import mercurial.node
        from breezy.plugins.hg.mapping import (
            mapping_registry,
            )
        parse_revid = getattr(branch.repository, "lookup_bzr_revision_id",
                              mapping_registry.parse_revision_id)
        branch.repository.lock_read()
        try:
            graph = branch.repository.get_graph()
            for revid, _ in graph.iter_ancestry([branch.last_revision()]):
                try:
                    foreign_revid, mapping = parse_revid(revid)
                except InvalidRevisionId:
                    continue
                if mercurial.node.hex(foreign_revid).startswith(csid):
                    if breezy_version < (2, 5):
                        history = branch.revision_history()
                        return RevisionInfo.from_revision_id(branch, revid, history)
                    else:
                        return RevisionInfo.from_revision_id(branch, revid)
            raise InvalidRevisionSpec(self.user_spec, branch)
        finally:
            branch.repository.unlock()

    def _match_on(self, branch, revs):
        loc = self.spec.find(':')
        csid = self.spec[loc+1:].encode("utf-8")
        if len(csid) > 40 or not valid_hg_csid(csid):
            raise InvalidRevisionSpec(self.user_spec, branch)
        from breezy.plugins.hg import (
            lazy_load_mercurial,
            )
        lazy_load_mercurial()
        if len(csid) == 40:
            return self._lookup_csid(branch, csid)
        else:
            return self._find_short_csid(branch, csid)

    def needs_branch(self):
        return True

    def get_branch(self):
        return None
