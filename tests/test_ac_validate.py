import os
import unittest

from asn1crypto import cms, x509

from pyhanko_certvalidator import ValidationContext
from pyhanko_certvalidator import validate
from .test_validate import MockFetcherBackend, fixtures_dir

attr_cert_dir = os.path.join(fixtures_dir, 'attribute-certs')
basic_aa_dir = os.path.join(attr_cert_dir, 'basic-aa')


def load_cert(fname):
    with open(fname, 'rb') as inf:
        return x509.Certificate.load(inf.read())


def load_attr_cert(fname):
    with open(fname, 'rb') as inf:
        return cms.AttributeCertificateV2.load(inf.read())


class ACValidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_basic_ac_validation_aacontrols_norev(self):
        ac = load_attr_cert(
            os.path.join(basic_aa_dir, 'aa', 'alice-role-norev.attr.crt')
        )

        root = load_cert(os.path.join(basic_aa_dir, 'root', 'root.crt'))
        interm = load_cert(os.path.join(
            basic_aa_dir, 'root', 'interm-role.crt')
        )
        role_aa = load_cert(
            os.path.join(basic_aa_dir, 'interm', 'role-aa.crt')
        )

        vc = ValidationContext(
            trust_roots=[root], other_certs=[interm, role_aa],
            fetcher_backend=MockFetcherBackend(),
        )

        result = await validate.async_validate_ac(ac, vc)
        assert len(result.aa_path) == 3
        assert 'role' in result.approved_attributes
        assert 'group' not in result.approved_attributes
