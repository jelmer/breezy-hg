# Copyright (C) 2006-2011 Canonical Ltd

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

def test_suite():
    from unittest import TestSuite
    from breezy.tests import TestUtil

    loader = TestUtil.TestLoader()

    suite = TestSuite()

    testmod_names = [
        'test_branch',
        'test_dir',
        'test_fetch',
        'test_idmap',
        'test_mapping',
        'test_parsers',
        'test_pull',
        'test_push',
        'test_repository',
        'test_revspec',
        ]

    suite.addTest(loader.loadTestsFromModuleNames(["%s.%s" % (__name__, i) for i in testmod_names]))

    return suite
