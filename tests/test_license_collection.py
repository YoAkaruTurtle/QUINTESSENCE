"""License collection must retain original terms and fail on missing evidence."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('collect_licenses',REPO/'apps/desktop/scripts/collect_licenses.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class LicenseCollectionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='license-collection-')
        self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.original=self.root/'LICENSE';self.original.write_bytes(b'Original copyright\r\nMIT terms\r\n')

    def test_original_copyright_and_line_endings_are_preserved(self):
        out=self.root/'out';row=module.copy_component(out,'example','1','MIT','https://example.invalid', [('LICENSE',self.original)])
        self.assertEqual((out/row['notices'][0]['file']).read_bytes(),self.original.read_bytes())
        self.assertEqual(row['notices'][0]['sha256'],module.digest(self.original))

    def test_missing_notices_stop_build(self):
        with self.assertRaisesRegex(ValueError,'missing original license'):
            module.copy_component(self.root/'out','example','1','MIT','https://example.invalid',[])

    def test_component_and_destination_cannot_escape_output(self):
        for name,label in [('../escape','LICENSE'),('example','../escape')]:
            with self.assertRaises(ValueError):
                module.copy_component(self.root/'out',name,'1','MIT','https://example.invalid',[(label,self.original)])

    def test_empty_license_or_missing_provenance_is_refused(self):
        self.original.write_bytes(b'')
        with self.assertRaises(ValueError):
            module.copy_component(self.root/'out','example','1','MIT','https://example.invalid',[('LICENSE',self.original)])
        with self.assertRaises(ValueError):
            module.copy_component(self.root/'out','example','1','MIT','',[('LICENSE',self.original)])

    def test_unreviewed_crate_cannot_borrow_a_generic_license(self):
        package=self.root/'crate';package.mkdir()
        doc={'packages':[{'name':'example','version':'2','source':'registry','manifest_path':str(package/'Cargo.toml'),'license':'MIT'}]}
        with self.assertRaisesRegex(ValueError,'review required'):
            module.cargo_components(REPO,self.root/'out',doc,{})

    def test_mpl_source_link_uses_exact_version(self):
        package=self.root/'crate';package.mkdir();(package/'LICENSE').write_text('MPL-2.0 original terms')
        doc={'packages':[{'name':'example','version':'1.2.3','source':'registry','manifest_path':str(package/'Cargo.toml'),'license':'MPL-2.0'}]}
        row=module.cargo_components(REPO,self.root/'out',doc,{})[0]
        self.assertTrue(row['source'].endswith('/example/1.2.3/download'))
        self.assertIn('Unmodified',row['source_availability'])

    def test_reviewed_license_survives_git_newline_normalization_but_not_text_changes(self):
        crlf=self.root/'CRLF';lf=self.root/'LF'
        crlf.write_bytes(b'Original copyright\r\nPermission terms\r\n')
        lf.write_bytes(b'Original copyright\nPermission terms\n')
        self.assertEqual(module.text_digest(crlf),module.text_digest(lf))
        lf.write_bytes(b'Other copyright\nPermission terms\n')
        self.assertNotEqual(module.text_digest(crlf),module.text_digest(lf))
