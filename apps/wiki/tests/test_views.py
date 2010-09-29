from nose.tools import eq_

from sumo.tests import TestCase
from wiki.models import VersionMetadata
from wiki.views import _version_groups


class TestVersionGroups(TestCase):
    def test_version_groups(self):
        """Make sure we correctly set up browser/version mappings for the JS"""
        versions = [VersionMetadata(1, 'Firefox 4.0', 'fx4', 5.0),
                    VersionMetadata(2, 'Firefox 3.5-3.6', 'fx35', 4.0),
                    VersionMetadata(4, 'Firefox Mobile 1.1', 'm11', 2.0)]
        want = {'fx': [(4.0, '35'), (5.0, '4')],
                'm': [(2.0, '11')]}
        eq_(want, _version_groups(versions))