"""Verify the public package without external services."""
import hashlib,sys
from pathlib import Path
sys.dont_write_bytecode=True
# A console without the Chinese code page prints escapes instead of failing.
if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(errors='backslashreplace')
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
# When invoked in the repository root, use its product source.
if (Path.cwd()/'src/corridor').is_dir():sys.path.insert(0,str(Path.cwd()/'src'))
# Recordings supplied with the submission materials, not with the repository.
EXTERNAL='EXTERNAL-ASSETS.sha256'
def manifest(name):
    rows={}
    for line in (ROOT/name).read_text(encoding='utf-8').splitlines():
        expected,path=line.split('  ',1)
        assert (ROOT/path).resolve().is_relative_to(ROOT),'invalid manifest path'
        assert path not in rows,'duplicate manifest path'
        rows[path]=expected
    return rows
def digest(name):return hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
def verify_hashes(external):
    listed=manifest('SHA256SUMS')
    assert not listed.keys()&external.keys(),'external asset listed as packaged'
    for name,expected in listed.items():assert digest(name)==expected,'hash mismatch: '+name
    actual={p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file() and p.name!='SHA256SUMS'}
    assert listed.keys()==actual-external.keys(),'missing or unlisted files'
    print(f'SHA256: {len(listed)} files match')
def verify_external(external):
    # An absent recording is reported, not counted as a failure; a present one must match.
    for name,expected in external.items():
        if (ROOT/name).is_file():
            assert digest(name)==expected,'hash mismatch: '+name
            print(f'External {name}: present; hash matches')
        else:print(f'External {name}: 随提交材料另附，未随仓库 (supplied with the submission materials, not in the repository); sha256 {expected}')
def main():
    external=manifest(EXTERNAL)
    verify_hashes(external);verify_external(external)
    from f9.recompute import verify as f9
    from mechanism.recompute import verify as mechanism
    from acceptance.replay import verify as acceptance
    f9();mechanism();acceptance()
    print('ALL CHECKS PASS; provider calls 0')
if __name__=='__main__':main()
