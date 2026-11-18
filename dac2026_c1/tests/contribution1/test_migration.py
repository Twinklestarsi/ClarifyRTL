import json
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from deployment.assets import pipeline_arguments,load_assets


class MigrationTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('numpy'),'optional NumPy packaging runtime')
    def test_build_cannot_overwrite_the_source_release(self):
        from deployment.build_release import release_stage
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td);project=parent/'dac2026_c1';project.mkdir()
            with self.assertRaises(ValueError):release_stage(project,parent)
            self.assertEqual(release_stage(project,project/'dist'),project/'dist/dac2026_c1')
    def test_model_defaults_resolve_from_the_relocated_root(self):
        with tempfile.TemporaryDirectory(prefix='relocated release ') as td:
            root=Path(td);(root/'models/encoder').mkdir(parents=True)
            (root/'models/best.npz').write_bytes(b'checkpoint')
            (root/'models/calibrator.json').write_text('{}')
            manifest=dict(checkpoint='models/best.npz',calibrator='models/calibrator.json',
                encoder_path='models/encoder',horizon=8,solver_timeout_ms=1000)
            (root/'models/manifest.json').write_text(json.dumps(manifest))
            args=pipeline_arguments(['relative_spec.md','--generation-dir','output'],root)
            self.assertEqual(args[0],'relative_spec.md')
            self.assertEqual(args[args.index('--c1-checkpoint')+1],str(root/'models/best.npz'))
            self.assertEqual(args[args.index('--c1-horizon')+1],'8')
            self.assertIn('--c1-bounded',args)

    def test_explicit_model_and_disabled_analysis_do_not_require_bundled_files(self):
        with tempfile.TemporaryDirectory() as td:
            custom=['spec.md','--c1-checkpoint=/custom/model.npz','--c1-horizon','2']
            self.assertEqual(pipeline_arguments(custom,td),custom)
            self.assertEqual(pipeline_arguments(['--help'],td),['--help'])
            self.assertEqual(pipeline_arguments(['--no-assumption-analysis'],td),['--no-assumption-analysis'])

    def test_manifest_cannot_resolve_assets_outside_the_release(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'models').mkdir()
            (root/'models/manifest.json').write_text(json.dumps(dict(checkpoint='../outside')))
            with self.assertRaises(ValueError):load_assets(root)

    def test_encoder_cache_links_are_materialized_without_changing_fingerprint(self):
        from assumption_modeling.schema import digest,file_hash
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'cache';source.mkdir();blob=root/'blob';blob.write_bytes(b'frozen weights')
            (source/'model.safetensors').symlink_to(blob)
            (source/'config.json').write_text('{}')
            target=root/'copied';shutil.copytree(source,target,symlinks=False)
            def fingerprint(path):return digest({str(f.relative_to(path)):file_hash(f) for f in sorted(path.rglob('*')) if f.is_file()})
            self.assertFalse((target/'model.safetensors').is_symlink())
            blob.unlink()
            self.assertEqual((target/'model.safetensors').read_bytes(),b'frozen weights')
            # Compare to the independent expected contents after the cache disappears.
            expected=root/'expected';expected.mkdir();(expected/'model.safetensors').write_bytes(b'frozen weights');(expected/'config.json').write_text('{}')
            self.assertEqual(fingerprint(target),fingerprint(expected))

if __name__=='__main__':unittest.main()
