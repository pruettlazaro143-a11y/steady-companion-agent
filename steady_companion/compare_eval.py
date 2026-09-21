"""One explicitly chosen scene; fixed configuration, isolated stores, shared cap."""
from dataclasses import asdict
import json,os,subprocess,sys,tempfile,zipfile,hashlib
from pathlib import Path
from types import SimpleNamespace
from .story_eval import evaluate_story,validate_story
from .provider import Config,ProviderError


def evaluate_comparison(args):
    story=validate_story(args.scenario)
    turns=sum('user' in s for s in story['steps'])
    if not 1<=turns<=8 or not 2<=args.max_calls<=32:
        raise ValueError('First comparison requires one short scene (1–8 user turns), total cap 2–32.')
    if args.baseline is None or not args.baseline.is_file(): raise ValueError('Use --baseline with your separately retained P1.1.1 ZIP; no historical archive is bundled or downloaded.')
    result={'status':'not_tested','actual_calls':0,'scenario_id':story.get('id'),'conditions':[],
            'model_quality_conclusion':None,'human_review':'required','cost':None}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as report:
        os.chmod(args.output,0o600)
        if not args.confirm_live:
            report.write(json.dumps(result,ensure_ascii=False,indent=2));print('对照输入已核验；未进行真实 API 测试。');return 0
        config=Config.from_env()
        if not config.api_key: raise ProviderError('Configure NEBIUS_API_KEY locally; no credential search is performed.')
        # Same explicit environment and defaults for both conditions. Never serialize the key.
        env=os.environ.copy()
        env.update(NEBIUS_MODEL=config.model,NEBIUS_BASE_URL=config.base_url,
                   NEBIUS_TIMEOUT=str(config.timeout),NEBIUS_MAX_TOKENS=str(config.max_tokens))
        with tempfile.TemporaryDirectory(prefix='steady-comparison-code-') as temp:
            root=Path(temp)
            with zipfile.ZipFile(args.baseline) as z:
                if len(z.infolist())>2000 or sum(i.file_size for i in z.infolist())>30000000: raise ValueError('Baseline archive exceeds bound')
                for info in z.infolist():
                    path=Path(info.filename)
                    if path.is_absolute() or '..' in path.parts or (info.external_attr>>16)&0o170000==0o120000: raise ValueError('Unsafe archive entry')
                z.extractall(root)
            baseline=root/'steady-companion-agent'
            version=(baseline/'steady_companion/__init__.py').read_text()
            if '__version__ = "0.4.2"' not in version: raise ValueError('Expected P1.1.1 Agent 0.4.2 baseline')
            current=Path(__file__).parent
            for rel in ('core.md','roles/R-A.json'):
                if (current/'skill'/rel).read_bytes()!=(baseline/'steady_companion/skill'/rel).read_bytes(): raise ValueError('Role/core mismatch; comparison refused')
            result['fixed_configuration']={'model':config.model,'parameters':{'max_tokens':config.max_tokens,'timeout':config.timeout},'role':'R-A','initial_memory':[], 'baseline_sha256':hashlib.sha256(args.baseline.read_bytes()).hexdigest()}
            for label,project in [('P1.1.1',baseline),('P1.2',current.parent)]:
                remaining=args.max_calls-result['actual_calls']
                if remaining<2: result['status']='budget_exhausted';break
                output=args.output.with_name(args.output.stem+'-'+label+'.jsonl').resolve()
                env['PYTHONPATH']=str(project.resolve())
                command=[sys.executable,'-m','steady_companion','eval-story','--scenario',str(args.scenario.resolve()),'--output',str(output),'--max-calls',str(remaining),'--confirm-live','--capture-stages']
                # Child processes read no real store; eval-story always creates a synthetic temp store.
                # No automatic retry, no shell expansion, no captured provider/private stdout.
                completed=subprocess.run(command,cwd=root,env=env,check=False)
                records=[json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []
                used=max((r.get('usage',{}).get('calls',0) for r in records),default=0)
                result['actual_calls']+=used
                result['conditions'].append({'condition':label,'output':str(output),'actual_calls':used,'exit_code':completed.returncode,'quality_result':None})
                if completed.returncode: result['status']='condition_incomplete_no_retry';break
            else:result['status']='completed_requires_human_review'
        report.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return int(result['status']!='completed_requires_human_review')
