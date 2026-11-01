"""Read-only authority checks. No credential or write capability is used here."""


from .contracts import (CONTROL_BRANCH, CONTROL_REPOSITORY, OWNER_ID, RECORDS_BRANCH,
    SHA, canonical_digest, fields, matches, positive_id, require, validate_registration)


def commit_ref(github, repository, ref):
    obj = github.ref(repository, ref)
    require(type(obj) is dict and obj.get('type') == 'commit' and matches(SHA, obj.get('sha')),
            'missing commit ref: ' + ref)
    return obj['sha']


def approved_registrations(github, control_sha):
    """The adapter reads the complete registry from this immutable control commit."""
    docs = github.list_registrations(CONTROL_REPOSITORY, control_sha)
    require(type(docs) is list, 'registry unavailable')
    seen = set()
    for doc in docs:
        c = validate_registration(doc)['configuration']
        claims = [('app',c['app']), ('actor',c['dispatcher_actor_id']),
                  ('dispatcher',c['dispatcher_app_id'])]
        claims += [(ch, dest['repository'].lower(), dest['path'].lower())
                   for ch,dest in c['destinations'].items()]
        for claim in claims:
            require(claim not in seen, 'overlapping app, path or dispatcher claim')
            seen.add(claim)
    return docs


def authorize_controller(registration, context, github):
    """Admit fresh workflow context using external run/ref/registry evidence."""
    registration = validate_registration(registration)
    c = registration['configuration']
    fields(context, 'repository ref sha run_id attempt mode role app')
    require(context['repository'] == CONTROL_REPOSITORY
            and context['ref'] == 'refs/heads/' + CONTROL_BRANCH
            and matches(SHA,context['sha']), 'untrusted controller repository/ref')
    require(context['app'] == c['app'], 'cross-app request')
    require(context['mode'] in {'prepare','register','inspect-baseline','approve-baseline','retry','status'},
            'unknown controller mode')
    require(context['role'] in {'authority','publish'}, 'unknown controller role')
    require(context['mode'] != 'register' or context['role'] == 'authority',
            'registration cannot publish')
    require(positive_id(context['run_id']) and positive_id(context['attempt']), 'invalid run locator')
    require(commit_ref(github, CONTROL_REPOSITORY, 'heads/' + CONTROL_BRANCH) == context['sha'],
            'stale controller revision; dispatch current control branch')
    run = github.run(CONTROL_REPOSITORY, context['run_id'], context['attempt'])
    expected = dict(id=context['run_id'],attempt=context['attempt'],repository=CONTROL_REPOSITORY,
        head_branch=CONTROL_BRANCH,head_sha=context['sha'],event='workflow_dispatch',
        workflow_path='.github/workflows/distribute.yml')
    require(all(run.get(k) == v for k,v in expected.items()), 'controller run context mismatch')
    actor = c['dispatcher_actor_id'] if context['mode'] == 'prepare' else OWNER_ID
    require(type(run.get('actor_id')) is int and type(run.get('triggering_actor_id')) is int
            and run['actor_id'] == actor == run['triggering_actor_id'], 'controller actor denied')
    if context['mode'] == 'prepare':
        bot = github.user(actor)
        require(bot.get('id') == actor and bot.get('type') == 'Bot'
                and matches(r'[A-Za-z0-9-]+\[bot\]',bot.get('login')), 'dispatcher bot mismatch')
        slug = bot['login'][:-5]
        app = github.app(slug)
        require(app.get('id') == c['dispatcher_app_id'] and app.get('slug') == slug,
                'dispatcher App mismatch')
    docs = approved_registrations(github,context['sha'])
    if context['mode'] != 'register':
        require(registration in docs, 'registration is not the protected approved version')
    else:
        # Prospective config may replace this app only; every other namespace stays owned.
        for doc in docs:
            other = doc['configuration']
            if other['app'] != c['app']:
                require(other['dispatcher_app_id'] != c['dispatcher_app_id']
                        and other['dispatcher_actor_id'] != c['dispatcher_actor_id'],
                        'dispatcher is already registered')
                for ch in c['destinations']:
                    require(other['destinations'][ch]['path'].lower() != c['destinations'][ch]['path'].lower(),
                            'destination already registered')
    return dict(controller_sha=context['sha'],configuration_digest=registration['configuration_digest'],
                app=c['app'],mode=context['mode'],role=context['role'],
                run_id=context['run_id'],attempt=context['attempt'],actor_id=actor,
                triggering_actor_id=actor)


def verify_release(registration, receipt, github):
    """Verify an independently downloaded run receipt, source run and current bytes."""
    import hashlib
    import io
    import zipfile
    from .snapshot import freeze, inspect_identity, strict_json

    registration = validate_registration(registration)
    c = registration['configuration']
    fields(receipt, 'schema_version repository tag version commit run_id attempt workflow_path '
           'workflow_revision workflow_sha256 event ref actor_id triggering_actor_id checksums_sha256 assets')
    require(type(receipt['schema_version']) is int and receipt['schema_version'] == 1,
            'unsupported receipt schema')
    require(receipt['repository'] == c['repository'] and positive_id(receipt['run_id'])
            and positive_id(receipt['attempt']), 'invalid receipt locator')
    require(receipt['workflow_path'] == c['release_workflow_path'], 'unregistered workflow')
    run = github.run(c['repository'],receipt['run_id'],receipt['attempt'])
    expected = dict(id=receipt['run_id'],attempt=receipt['attempt'],repository=c['repository'],
        workflow_path=c['release_workflow_path'],head_sha=receipt['commit'],event='push',
        head_branch=receipt['tag'],actor_id=OWNER_ID,triggering_actor_id=OWNER_ID,
        status='completed',conclusion='success')
    require(all(run.get(k) == v for k,v in expected.items()), 'release run denied')
    require(receipt['actor_id'] == OWNER_ID == receipt['triggering_actor_id']
            and receipt['event'] == 'push' and receipt['ref'] == 'refs/tags/' + receipt['tag'], 'receipt actors/event denied')
    require(receipt['workflow_revision'] == receipt['commit'] and matches(SHA,receipt['commit']),
            'workflow revision mismatch')
    workflow = github.file(c['repository'],receipt['commit'],c['release_workflow_path'])
    require(workflow.get('type') == 'file' and type(workflow.get('content')) is bytes,
            'workflow must be a regular file')
    require(hashlib.sha256(workflow['content']).hexdigest() == receipt['workflow_sha256'],
            'workflow audit hash mismatch')
    artifact = github.receipt_artifact(c['repository'],receipt['run_id'],receipt['attempt'])
    require(positive_id(artifact.get('id')) and artifact.get('expired') is False
            and artifact.get('name') == f"distribution-receipt-{receipt['run_id']}-{receipt['attempt']}"
            and artifact.get('run_id') == receipt['run_id'] and artifact.get('head_sha') == receipt['commit'],
            'receipt artifact provenance mismatch')
    content = artifact.get('content')
    require(type(content) is bytes and len(content) <= 1024*1024
            and hashlib.sha256(content).hexdigest() == artifact.get('sha256'), 'artifact digest mismatch')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        require(archive.namelist() == ['receipt.json'], 'unexpected receipt archive members')
        info = archive.getinfo('receipt.json')
        require(info.file_size <= 1024*1024 and (info.external_attr >> 16) & 0o170000 != 0o120000,
                'unsafe receipt archive member')
        actual = strict_json(archive.read(info))
    require(actual == receipt, 'receipt differs from run-bound artifact')
    identity = inspect_identity(registration,receipt['tag'],github)
    for field in ['repository','tag','version','commit','checksums_sha256','assets']:
        require(receipt[field] == identity[field], 'receipt/current identity conflict: ' + field)
    reference = dict(repository=c['repository'],run_id=receipt['run_id'],attempt=receipt['attempt'],
        artifact_id=artifact['id'],artifact_sha256=artifact['sha256'],
        workflow_path=c['release_workflow_path'],workflow_revision=receipt['workflow_revision'],
        workflow_sha256=receipt['workflow_sha256'])
    return freeze(registration,identity,dict(kind='release-run',reference=reference,
        owner_id=OWNER_ID,identity_digest=canonical_digest(identity)))
