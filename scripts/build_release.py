"""Build only the reviewed manifest, offline. Run after tests and source review."""
from pathlib import Path,PurePosixPath
import hashlib,json,os,shutil,subprocess,sys,tempfile,zipfile
ROOT=Path(__file__).resolve().parents[1]
VERSION='0.6.0.dev15';SKILL='0.8.0-dev15'

def sha(data):return hashlib.sha256(data).hexdigest()
def archive(path,items):
    with zipfile.ZipFile(path,'x',zipfile.ZIP_DEFLATED) as z:
        for name,body,executable in sorted(items):
            info=zipfile.ZipInfo(name,(2026,9,21,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=(0o100755 if executable else 0o100644)<<16;z.writestr(info,body)

def main():
    output=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else ROOT/'dist'
    manifest=json.loads((ROOT/'PUBLIC-MANIFEST.json').read_text());files=manifest['files'];selected=[]
    for name,digest in files.items():
        rel=PurePosixPath(name)
        if rel.is_absolute() or '..' in rel.parts or '.git' in rel.parts:raise ValueError('Invalid manifest path')
        path=ROOT/name
        if path.is_symlink() or not path.is_file() or sha(path.read_bytes())!=digest:raise ValueError('Manifest mismatch: '+name)
        selected.append((name,path.read_bytes(),name.endswith('.command')))
    selected.append(('PUBLIC-MANIFEST.json',(ROOT/'PUBLIC-MANIFEST.json').read_bytes(),False))
    wheel_name='steady_companion_agent-'+VERSION+'-py3-none-any.whl'
    source_name='steady-companion-agent-'+VERSION+'-source.zip';skill_name='steady-companion-skill-'+SKILL+'.zip'
    for name in (wheel_name,source_name,skill_name,'SHA256SUMS','ARTIFACT_CHECKS.json'):
        if (output/name).exists():raise ValueError('Refusing existing artifact: '+name)
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='steady-public-build-') as temp:
        staging=Path(temp)/'source';staging.mkdir()
        for name,body,_ in selected:
            f=staging/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(body)
        env={'PATH':'/usr/bin:/bin','PIP_CONFIG_FILE':'/dev/null','PIP_DISABLE_PIP_VERSION_CHECK':'1','PIP_NO_CACHE_DIR':'1'}
        # Fixed UTC archive date, independent of the build host timezone.
        from datetime import datetime,timezone
        env['SOURCE_DATE_EPOCH']=str(int(datetime(2026,9,21,tzinfo=timezone.utc).timestamp()))
        subprocess.run([sys.executable,'-m','pip','wheel','--no-index','--no-deps','--no-build-isolation','--wheel-dir',str(output),'.'],cwd=staging,env=env,check=True)
    package=[(name,body) for name,body,_ in selected if name.startswith('steady_companion/')]
    with zipfile.ZipFile(output/wheel_name) as z:
        actual={n for n in z.namelist() if n.startswith('steady_companion/') and not n.endswith('/')}
        if actual!={name for name,_ in package}:raise ValueError('Unexpected/missing wheel files')
        for name,body in package:
            if z.read(name)!=body:raise ValueError('Wheel byte mismatch')
    skill=[('steady-companion/'+name.split('steady_companion/skill/',1)[1],body,False) for name,body,_ in selected if name.startswith('steady_companion/skill/')]
    archive(output/skill_name,skill)
    archive(output/source_name,[('steady-companion-agent-'+VERSION+'/'+name,body,executable) for name,body,executable in selected])
    report={'version':VERSION,'source_manifest_sha256':sha((ROOT/'PUBLIC-MANIFEST.json').read_bytes()),'source_files':len(selected),'wheel_files_byte_identical':len(package),'skill_files_byte_identical':len(skill),'build':'offline no-index no-deps no-build-isolation','real_api_calls':0}
    (output/'ARTIFACT_CHECKS.json').write_text(json.dumps(report,indent=2)+'\n')
    (output/'SHA256SUMS').write_text(''.join(sha((output/n).read_bytes())+'  '+n+'\n' for n in (source_name,wheel_name,skill_name,'ARTIFACT_CHECKS.json')))
    print(json.dumps(report))
if __name__=='__main__':main()
