"""Maintainer CLI and protected workflow entry point."""
import argparse
import os
from pathlib import Path
import sys

from . import controller
from .contracts import CONTROL_REPOSITORY, DIGEST, VERSION, matches, require, validate_registration
from .github import AppCredentials, GitHub, RefOutcomeUnknown
from .snapshot import strict_json
from .state import encode
from .validation import read_regular


def positive(value):
    import re
    require(type(value) is str and re.fullmatch(r'[1-9][0-9]{0,19}',value), 'invalid run/attempt')
    return int(value)


def adapter(mode,app):
    env = os.environ
    context = dict(repository=env.get('GITHUB_REPOSITORY'),ref=env.get('GITHUB_REF'),sha=env.get('GITHUB_SHA'),
        run_id=positive(env.get('GITHUB_RUN_ID')),attempt=positive(env.get('GITHUB_RUN_ATTEMPT')),
        mode=mode,app=app,role=env.get('DISTRIBUTION_ROLE'))
    return GitHub(context=context,environment=env.get('DISTRIBUTION_ENVIRONMENT'))


def workflow_arguments(argv):
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    event=strict_json(read_regular(Path(os.environ['GITHUB_EVENT_PATH'])))
    inputs=event.get('inputs')
    require(type(inputs) is dict and all(type(v) is str for v in inputs.values()), 'workflow inputs must be strings')
    allowed={'mode','app','run_id','attempt','tag','version','channel','candidate','approve_sha'}
    require(set(inputs) <= allowed, 'unknown workflow inputs')
    mode=inputs.get('mode')
    require(mode in {'register','prepare','inspect-baseline','approve-baseline','retry','status'}, 'unknown workflow mode')
    require(matches(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*',inputs.get('app')), 'invalid requested app')
    if mode == 'prepare':
        positive(inputs.get('run_id'))
        positive(inputs.get('attempt'))
    if mode == 'retry':
        require(inputs.get('channel') in {'brew','scoop','winget'}, 'retry requires one valid channel')
    if mode in {'retry','status'}:
        require(matches(VERSION,inputs.get('version')), 'invalid requested version')
    if mode == 'inspect-baseline':
        require(matches('v'+VERSION,inputs.get('tag')), 'invalid requested stable tag')
    if mode in {'register','approve-baseline'}:
        require(matches(DIGEST,inputs.get('approve_sha')), 'explicit approval digest required')
    command=[mode,'--out',args.out]
    if mode in {'register','approve-baseline'}:
        candidate=strict_json(inputs.get('candidate',''))
        require(candidate['app'] == inputs.get('app'), 'candidate/request app mismatch')
        path=Path(args.out).parent/'distribution-request-candidate.json'
        require(not path.is_symlink(), 'candidate output symlink denied')
        path.write_bytes(encode(candidate))
        command += ['--candidate',str(path),'--approve-sha',inputs.get('approve_sha','')]
    else:
        command += ['--app',inputs.get('app','')]
    if mode == 'prepare':
        command += ['--run-id',inputs.get('run_id',''),'--attempt',inputs.get('attempt','')]
    if mode == 'inspect-baseline':
        command += ['--tag',inputs.get('tag','')]
    if mode in {'retry','status'}:
        command += ['--version',inputs.get('version','')]
    if mode == 'retry':
        command += ['--channel',inputs.get('channel','')]
    return command


def observe_source(argv, *, github=None, command=None, runner=None):
    """Secret-free workflow entry: protected registration supplies the query identity."""
    from .github import collect_windows_source
    parser=argparse.ArgumentParser(description='Observe registered WinGet default-source versions')
    parser.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    try:
        request=workflow_arguments(argv)
        event=strict_json(read_regular(Path(os.environ['GITHUB_EVENT_PATH'])))
        mode,app=request[0],event['inputs']['app']
        github=github or adapter(mode,app)
        context=github.controller_context()
        require(context['mode'] == mode and context['app'] == app, 'source/request context mismatch')
        entry=github.file(CONTROL_REPOSITORY,context['sha'],f'.distribution/registrations/{app}.json')
        require(entry['type'] == 'file', 'protected registration must be regular')
        registration=validate_registration(strict_json(entry['content']))
        document=collect_windows_source(registration,github,command=command,runner=runner)
    except Exception as error:
        document=dict(state='unknown',failure_code='observation-unavailable',message=str(error))
    path=Path(args.out)
    require(not path.is_symlink(), 'output symlink denied')
    path.write_bytes(encode(document))
    print(encode(document).decode())
    return 1 if document.get('state') == 'unknown' else 0


def main(argv=None, *, github=None, credentials=None, source_command=None, source_runner=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == '--observe-source':
        return observe_source(argv[1:],github=github,command=source_command,runner=source_runner)
    if argv and argv[0] == '--workflow':
        try:
            argv=workflow_arguments(argv[1:])
        except Exception as error:
            document=dict(state='failed',failure_code='request-denied',message=str(error))
            if '--out' in argv:
                path=Path(argv[argv.index('--out')+1])
                require(not path.is_symlink(), 'output symlink denied')
                path.write_bytes(encode(document))
            print(encode(document).decode())
            return 1
    if argv and argv[0] == 'validate':
        from .validation import main as validate
        return validate(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode',required=True)
    command = sub.add_parser('register')
    command.add_argument('--candidate',required=True)
    command.add_argument('--approve-sha',required=True)
    command.add_argument('--out',required=True)
    for name in ['prepare','inspect-baseline','approve-baseline','retry','status']:
        command = sub.add_parser(name)
        command.add_argument('--out')
        if name == 'approve-baseline':
            command.add_argument('--candidate',required=True)
            command.add_argument('--approve-sha',required=True)
        else:
            command.add_argument('--app',required=True)
        if name == 'prepare':
            command.add_argument('--run-id',required=True,type=positive)
            command.add_argument('--attempt',required=True,type=positive)
        if name == 'inspect-baseline':
            command.add_argument('--tag',required=True)
        if name in {'retry','status'}:
            command.add_argument('--version',required=True)
        if name == 'retry':
            command.add_argument('--channel',required=True,choices=['brew','scoop','winget'])
    args = parser.parse_args(argv)
    document = None
    credential_owner = credentials
    output = None
    try:
        candidate = strict_json(read_regular(Path(args.candidate))) if hasattr(args,'candidate') else None
        app = candidate['app'] if args.mode == 'register' else (candidate['app'] if args.mode == 'approve-baseline' else args.app)
        require(matches(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*',app), 'invalid requested app')
        github = github or adapter(args.mode,app)
        context = github.controller_context()
        require(context['mode'] == args.mode and context['app'] == app, 'CLI/request context mismatch')
        if args.mode == 'register':
            document = controller.register(candidate,args.approve_sha,github)
            output = document
        else:
            entry = github.file(CONTROL_REPOSITORY,context['sha'],f'.distribution/registrations/{app}.json')
            require(entry['type'] == 'file', 'protected registration must be regular')
            registration = validate_registration(strict_json(entry['content']))
            credential_owner = credentials or AppCredentials(github,registration)
            if context['role'] == 'publish':
                if args.mode == 'status':
                    document = controller.status(registration,args.version,github,credential_owner)
                else:
                    require(args.mode in {'prepare','approve-baseline','retry'}, 'unsupported publisher operation')
                    ref = strict_json(os.environ.get('DISTRIBUTION_RECORD_REF',''))
                    channels = [args.channel] if args.mode == 'retry' else None
                    document = controller.publish(registration,ref,github,credential_owner,channels=channels)
            else:
                request = {}
                if args.mode == 'prepare':
                    request = dict(run_id=args.run_id,attempt=args.attempt)
                elif args.mode == 'inspect-baseline':
                    request = dict(tag=args.tag)
                elif args.mode == 'approve-baseline':
                    request = dict(candidate=candidate,approve_sha=args.approve_sha)
                elif args.mode in {'retry','status'}:
                    request = dict(version=args.version)
                document = controller.authority(registration,request,github,credential_owner)
            output = document['candidate'] if args.mode == 'inspect-baseline' else document
    except Exception as error:
        code='identity-conflict' if isinstance(error,controller.IdentityConflict) or str(error).startswith('frozen identity') else 'admission-or-operation-unavailable'
        document = output = dict(state='unknown' if isinstance(error,RefOutcomeUnknown) else 'failed',
            failure_code='write-outcome-unknown' if isinstance(error,RefOutcomeUnknown) else code,message=str(error))
        if isinstance(error,RefOutcomeUnknown):
            document['refs']=error.refs
    finally:
        if credential_owner:
            errors = credential_owner.close()
            if errors:
                document['credential_errors'] = errors
                document['state'] = 'partial-failure'
    if args.out:
        target = Path(args.out)
        require(not target.is_symlink(), 'output symlink denied')
        target.write_bytes(encode(output))
    if document.get('record_ref') and os.environ.get('GITHUB_OUTPUT'):
        import json
        with open(os.environ['GITHUB_OUTPUT'],'a') as stream:
            stream.write('record_ref='+json.dumps(document['record_ref'],sort_keys=True,separators=(',',':'))+'\n')
    message = encode(document).decode()
    print(message)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as stream:
            stream.write('```json\n'+message+'```\n')
    return 1 if document.get('state') in {'failed','partial-failure','unknown','rejected'} else 0


if __name__ == '__main__':
    sys.exit(main())
