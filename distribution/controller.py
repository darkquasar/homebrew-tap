"""Admitted release distribution and channel publication."""
import hashlib
import io
import zipfile

from .contracts import CONTROL_REPOSITORY, DIGEST, OWNER_ID, RECORDS_BRANCH, VERSION, canonical_digest, fields, matches, require, validate_configuration, validate_registration
from .snapshot import approve_snapshot, inspect_identity, load_authorized_snapshot, strict_json, validate_snapshot
from .state import encode, file_digest, regular, result, save_status, version_key
from .validation import expected_paths, render, validate_candidate
from .trust import authorize_controller, verify_release
from . import bottles as bottle_rules


class IdentityConflict(ValueError):
    """An already frozen app/version cannot acquire different binary identity."""


def register(configuration, approve_sha, github):
    validate_configuration(configuration)
    digest = canonical_digest(configuration)
    require(digest == approve_sha, 'configuration differs from owner-approved digest')
    context = github.controller_context()
    require(context['mode'] == 'register', 'registration requires owner register mode')
    provisional = dict(schema_version=1,configuration=configuration,configuration_digest=digest,
        approval=dict(owner_id=OWNER_ID,configuration_digest=digest,reference='pending authenticated owner admission'))
    admission = admit(provisional,github,'authority')
    provisional['approval']['reference'] = approval_reference(admission)
    return validate_registration(provisional)


def status(registration,version,github,credentials):
    admission = admit(registration,github,'publish')
    require(admission['mode'] == 'status', 'status requires authenticated owner mode')
    snapshot = status_snapshot(registration,version,github)
    require(inspect_identity(registration,'v'+version,github) == snapshot['payload']['identity'],
            'frozen identity conflict')
    outcomes = {}
    for channel in ['brew','scoop']:
        outcome = result(channel,registration,snapshot)
        try:
            c = registration['configuration']
            repo = c['destinations'][channel]['repository']
            branch = github.default_branch(repo)
            tip = github.branch_tip(repo,branch)
            marker_path = '.distribution/channels/'+c['app']+'.json'
            paths = [c['destinations'][channel]['path'],marker_path]
            tree = github.tree(repo,tip,paths=paths)
            raw = regular(tree,marker_path)
            require(raw is not None, 'no published channel marker')
            marker = strict_json(raw)
            binding = verify_marker(channel,registration,snapshot,marker,tree,github)
            state = 'published' if marker['version'] == version else 'skipped'
            outcome.update(state=state,marker_observation=binding,definition_digest=marker['definition_digest'],
                destination_commit=tip,message='Actual current definition observed at '+marker['version'])
            outcome.update({k: marker[k] for k in ['release_tag','bottle_set_sha256','bottles'] if k in marker})
        except Exception as error:
            outcome.update(state='unknown',failure_code='observation-unavailable',message=str(error))
        outcomes[channel] = outcome
    outcomes['winget'] = winget_status(registration,snapshot,github)
    credentials.acquire('publish',admission,repository=CONTROL_REPOSITORY)
    refreshed,storage_errors = refresh_pending(registration,version,github)
    for channel,outcome in outcomes.items():
        outcome['admission'] = admission
        try:
            save_status(registration,snapshot,{channel:outcome},github)
        except Exception as error:
            storage_errors.append(str(error))
    return dict(state='partial-failure' if storage_errors or any(v['state'] in {'unknown','rejected','failed'}
        for v in outcomes.values()) else 'complete',admission=admission,channels=outcomes,
        refreshed=refreshed,storage_errors=storage_errors)


def status_snapshot(registration,version,github):
    require(matches(VERSION,version), 'invalid status version')
    tip = github.branch_tip(CONTROL_REPOSITORY,RECORDS_BRANCH)
    require(tip is not None, 'protected records unavailable')
    path = f".distribution/releases/{registration['configuration']['app']}/{version}.json"
    entry = github.file(CONTROL_REPOSITORY,tip,path)
    require(entry['type'] == 'file', 'protected record must be regular')
    snapshot = strict_json(entry['content'])
    validate_snapshot(registration,snapshot)
    require(snapshot['payload']['identity']['version'] == version, 'record version/path mismatch')
    return snapshot


def refresh_pending(registration,current_version,github):
    refreshed = {}
    errors = []
    try:
        versions = github.release_versions(registration)
    except Exception as error:
        return refreshed,['prior release lookup unavailable: '+str(error)]
    for version in versions:
        if version == current_version:
            continue
        try:
            cached = github.status_cache(registration,version).get('channels',{}).get('winget',{})
            if cached.get('state') == 'published':
                continue
            snapshot = status_snapshot(registration,version,github)
            observation = winget_status(registration,snapshot,github)
            save_status(registration,snapshot,{'winget':observation},github)
            refreshed[version] = observation
        except Exception as error:
            errors.append(f'prior {version} observation/state unavailable: {error}')
    return refreshed,errors


def winget_status(registration,snapshot,github):
    c = registration['configuration']
    version = snapshot['payload']['identity']['version']
    outcome = result('winget',registration,snapshot)
    try:
        observed = observe_winget(registration,version,github,github.source)
        outcome.update(observed)
        if observed['state'] != 'generated':
            return outcome
        head = observed['refs'].get('current_head')
        if not head:
            return {**outcome,'message':'No prepared branch or upstream submission observed'}
        repo = c['destinations']['winget']['repository']
        candidate = render('winget',registration,snapshot)
        base = github.merge_base(github.upstream()['sha'],repo,head)
        require(github.diff_paths(repo,base,head) <= set(candidate), 'unregistered branch diff')
        tree = github.tree(repo,head,paths=list(candidate))
        actual = {p:regular(tree,p) for p in candidate}
        evidence = validate_candidate('winget',registration,snapshot,actual)
        require(evidence['passed'], 'existing branch is not a validated candidate')
        return {**outcome,'state':'prepared','validation':evidence,'definition_digest':file_digest(actual),
                'destination_commit':head,'refs':dict(outcome['refs'],upstream_base=base,head=head)}
    except Exception as error:
        return {**outcome,'state':'unknown','failure_code':'observation-unavailable','message':str(error)}


def observe_winget(registration, version, github, source):
    """Observe exact head submissions. Errors retain prior refs and never imply visibility."""
    c = validate_registration(registration)['configuration']
    require(matches(VERSION,version), 'invalid observation version')
    branch = c['app']+'/'+version
    prior = {}
    cache_warning = None
    try:
        prior = github.status_cache(registration,version).get('channels',{}).get('winget',{})
        require(type(prior) is dict and type(prior.get('refs',{})) is dict, 'malformed channel cache')
    except Exception as error:
        prior = {}
        cache_warning = str(error)
    try:
        refs = dict(prior.get('refs',{}))
        if cache_warning:
            refs['cache_warning'] = cache_warning
        prs = github.pull_requests(registration,version)
        require(type(prs) is list, 'PR observation unavailable')
        for pr in prs:
            require(pr['head_repository'] == c['destinations']['winget']['repository']
                    and pr['head_branch'] == branch, 'cross-app PR observation')
        refs['pull_requests'] = prs
        head = github.branch_tip(c['destinations']['winget']['repository'],branch)
        refs['branch'] = branch
        refs['current_head'] = head
        merged = [pr for pr in prs if pr['merged']]
        opened = [pr for pr in prs if pr['state'] == 'open']
        if prs:
            active = (merged or opened or prs)[-1]
            previous_pr = refs.get('submitted_pr')
            if previous_pr != active['number']:
                refs['submitted_head'] = active['head_sha']
            refs['submitted_pr'] = active['number']
            refs['externally_edited'] = head is not None and head not in {
                refs['submitted_head'],refs.get('controller_prepared_head')}
        if merged:
            observation = source.observe(registration,version)
            require(observation.get('verified') is True and observation.get('source') == 'winget'
                    and observation.get('runner_os') == 'Windows'
                    and observation.get('package_id') == c['identifiers']['winget'], 'invalid source observation')
            return dict(state='published' if version in observation['versions'] else 'accepted-awaiting-indexing',
                        refs=refs,source_observation=observation)
        if opened:
            return dict(state='submitted',refs=refs)
        if prs:
            if prior.get('state') == 'prepared' and head and head == refs.get('controller_prepared_head'):
                # Public cache is a hint only: callers must validate actual branch bytes below.
                return dict(state='generated',refs=refs,closed_retry=True)
            return dict(state='rejected',refs=refs,message='Closed unmerged; owner retry and browser submission required')
        return dict(state='generated',refs=refs)
    except Exception as error:
        return dict(state='unknown',refs=locals().get('refs',prior.get('refs',{})),
                    last_observation=prior,message=str(error),failure_code='observation-unavailable')


def publish(registration, record_ref, github, credentials, channels=None, *, evidence=None, archives=None):
    """Publish/prepare admitted channels that passed native checks (PD-17).

    `evidence` is the parsed native-checks.json of this run (None when absent) and
    `archives` maps remote bottle filenames to bytes from the bottle build/select
    jobs. Both are untrusted inputs: every binding is re-derived here.
    """
    admission = admit(registration, github, 'publish')
    snapshot = load_authorized_snapshot(registration, record_ref, github)
    channels = ['brew','scoop','winget'] if channels is None else channels
    require(channels and len(set(channels)) == len(channels)
            and set(channels) <= {'brew','scoop','winget'}, 'invalid requested channels')
    context = github.controller_context()
    native_reference = None
    if type(evidence) is dict and type(evidence.get('run')) is dict:
        native_reference = dict(run=evidence['run'], digest=canonical_digest(evidence))
    # Native tools/dependencies bootstrap before minting any publishing credential.
    candidates = {}
    selected_bottles = {}
    outcomes = {}
    for channel in channels:
        bottles = None
        try:
            if channel == 'brew':
                bottles = evidence_bottle_set(registration, snapshot, evidence)
            candidate = render(channel, registration, snapshot, bottles)
            binding = (hashlib.sha256(candidate[registration['configuration']['destinations']['brew']['path']]).hexdigest()
                       if channel == 'brew' else file_digest(candidate))
            failing = bottle_rules.native_failures(channel, registration, snapshot, evidence, binding,
                                                   context, bottles)
            if failing:
                outcomes[channel] = native_failed(channel, registration, snapshot, failing, native_reference)
                continue
            evidence_result = validate_candidate(channel, registration, snapshot, candidate, bottles)
        except NativeEvidenceUnavailable as error:
            outcomes[channel] = native_failed(channel, registration, snapshot, [str(error)], native_reference)
            continue
        except Exception as error:
            outcomes[channel] = result(channel,registration,snapshot,state='failed',
                failure_code='validation-unavailable' if isinstance(error,(ImportError,FileNotFoundError)) else 'validation-failed',
                message=str(error))
            continue
        if evidence_result['passed']:
            candidates[channel] = candidate
            selected_bottles[channel] = bottles
        else:
            outcomes[channel] = result(channel, registration, snapshot, state='failed',
                validation=evidence_result, failure_code=evidence_result['failure_code'], message='; '.join(evidence_result['errors']))
    storage_errors = []
    try:
        credentials.acquire('publish',admission,repository=CONTROL_REPOSITORY)
    except Exception as error:
        storage_errors.append('status credential unavailable: '+str(error))
    refreshed,refresh_errors = refresh_pending(registration,snapshot['payload']['identity']['version'],github)
    storage_errors.extend(refresh_errors)
    for channel in channels:
        if channel in candidates:
            try:
                credentials.acquire('publish',admission,repository=registration['configuration']['destinations'][channel]['repository'])
                if channel == 'brew':
                    released = apply_bottles(registration, snapshot, selected_bottles['brew'], archives or {},
                                             evidence, github)
                    if 'state' in released:
                        outcomes[channel] = released
                    else:
                        outcomes[channel] = apply_channel(channel, registration, snapshot, candidates[channel],
                                                          github, bottles=released)
                else:
                    outcomes[channel] = apply_channel(channel, registration, snapshot, candidates[channel], github)
            except Exception as error:
                from .github import RefOutcomeUnknown
                outcomes[channel] = result(channel, registration, snapshot,
                    state='unknown' if isinstance(error,RefOutcomeUnknown) else 'failed',
                    refs=error.refs if isinstance(error,RefOutcomeUnknown) else {},
                    definition_digest=file_digest(candidates[channel]),
                    failure_code=('write-outcome-unknown' if isinstance(error,RefOutcomeUnknown) else
                        'identity-conflict' if isinstance(error,IdentityConflict) else 'destination-unavailable'),message=str(error))
        outcomes[channel]['admission'] = admission
        if native_reference:
            outcomes[channel]['native_checks'] = native_reference
        try:
            save_status(registration, snapshot, {channel: outcomes[channel]}, github)
        except Exception as error:
            storage_errors.append(str(error))
    return dict(state='partial-failure' if storage_errors or any(v['state'] in
        {'failed','unknown','rejected'} for v in outcomes.values()) else 'complete',
        admission=admission, channels=outcomes, refreshed=refreshed,storage_errors=storage_errors)


class NativeEvidenceUnavailable(ValueError):
    """The run's native evidence cannot supply a checked bottle_set."""


def evidence_bottle_set(registration, snapshot, evidence):
    """The bottle_set the native checks covered; the only set Brew may publish."""
    try:
        document = bottle_rules.validate_native_checks(evidence)
    except bottle_rules.NativeEvidenceInvalid as error:
        raise NativeEvidenceUnavailable('native-checks.json ' + ('missing' if evidence is None else
                                        'invalid: ' + str(error))) from None
    if document['brew'] is None:
        raise NativeEvidenceUnavailable('brew evidence missing')
    try:
        return bottle_rules.check_bottle_set(registration, snapshot, document['brew']['bottle_set'])
    except bottle_rules.BottleInvalid as error:
        raise NativeEvidenceUnavailable('brew evidence bottle_set invalid: ' + str(error)) from None


def native_failed(channel, registration, snapshot, failing, reference=None):
    outcome = result(channel, registration, snapshot, state='failed', failure_code='native-check-failed',
                     failing_platforms=list(failing),
                     message='Native package checks did not pass: ' + '; '.join(failing))
    if reference:
        outcome['native_checks'] = reference
    return outcome


def bottle_failed(registration, snapshot, code, message, **changes):
    return result('brew', registration, snapshot, state='failed', failure_code=code, message=message, **changes)


def apply_bottles(registration, snapshot, bottles, archives, evidence, github):
    """Make the checked bottle_set durable on the tap release (plan T6 steps 3 to 7).

    Returns the published bottle_set, or a failed Brew channel result. Nothing is
    written before every check passes; only this run's own draft is ever changed.
    """
    try:
        registration = validate_registration(registration)
        validate_snapshot(registration, snapshot)
        c = registration['configuration']
        identity = snapshot['payload']['identity']
        version = identity['version']
        try:
            bottles = bottle_rules.check_bottle_set(registration, snapshot, bottles)
        except bottle_rules.BottleInvalid as error:
            return bottle_failed(registration, snapshot, 'bottle-invalid', str(error))
        formula = render('brew', registration, snapshot, bottles)[c['destinations']['brew']['path']]
        failing = bottle_rules.native_failures('brew', registration, snapshot, evidence,
            hashlib.sha256(formula).hexdigest(), github.controller_context(), bottles)
        if failing:
            return native_failed('brew', registration, snapshot, failing)
        # Step 3: artifacts from unprivileged jobs are untrusted; re-verify every archive.
        if type(archives) is not dict:
            return native_failed('brew', registration, snapshot, ['bottle archives missing'])
        upload = {}
        for tag, digest in bottles['tags'].items():
            name = bottle_rules.remote_filename(c['identifiers']['brew'], version, tag)
            platform = '-'.join(bottle_rules.bottle_tag_platform(tag))
            data = archives.get(name)
            if type(data) is not bytes or hashlib.sha256(data).hexdigest() != digest:
                return native_failed('brew', registration, snapshot,
                                     [f'{platform}: archive {name} missing or differs from the checked bottle'])
            try:
                bottle_rules.verify_bottle(data, registration, snapshot, tag)
            except bottle_rules.BottleConflict as error:
                return bottle_failed(registration, snapshot, 'bottle-conflict', str(error), failing_platforms=[platform])
            except bottle_rules.BottleInvalid as error:
                return bottle_failed(registration, snapshot, 'bottle-invalid', str(error), failing_platforms=[platform])
            upload[name] = data
        source = evidence['brew']['source']
        # Steps 1/4: a published release is selected as is, never rebuilt or replaced.
        try:
            published = bottle_rules.select_bottles(registration, snapshot, github)
        except bottle_rules.BottleConflict as error:
            return bottle_failed(registration, snapshot, 'bottle-conflict', str(error))
        if published is not None:
            if published['bottle_set'] != bottles:
                code = 'bottle-race' if source == 'fresh' else 'bottle-conflict'
                return bottle_failed(registration, snapshot, code,
                    f"tap release {bottles['release_tag']} already publishes a different bottle set; "
                    'retry selects and checks the published set')
            return published['bottle_set']
        if source != 'fresh':
            return bottle_failed(registration, snapshot, 'bottle-conflict',
                f"checked published release {bottles['release_tag']} is no longer visible")
        # Steps 5/6: a draft owned by this run, read back, then published under the tag.
        context = github.controller_context()
        body = (f"Homebrew bottles for {c['app']} {version}.\n\nrun_id: {context['run_id']}\n"
                f"run_attempt: {context['attempt']}\nbottle_set_sha256: {canonical_digest(bottles)}\n")
        repository = c['destinations']['brew']['repository']
        target = github.branch_tip(repository, github.default_branch(repository))
        require(target is not None, 'tap default branch unavailable')
        from .github import ReleaseTagTaken
        try:
            draft = github.create_bottle_draft(registration, version, body, target)['id']
        except ReleaseTagTaken as error:
            return bottle_failed(registration, snapshot, 'bottle-race',
                f'{error}; no formula written, retry selects and checks the published set')
        try:
            for tag in bottle_rules.ordered_tags(bottles['tags']):
                name = bottle_rules.remote_filename(c['identifiers']['brew'], version, tag)
                github.upload_bottle(registration, version, draft, name, upload[name])
            require(github.draft_assets(registration, version, draft) == upload, 'draft asset read-back mismatch')
            github.publish_bottle_draft(registration, version, draft)
        except Exception as error:
            published_own = False
            if not isinstance(error, ReleaseTagTaken):
                try:
                    current = github.published_release(registration, version)
                    published_own = current is not None and current['id'] == draft
                except Exception:
                    published_own = False
            if not published_own:
                cleanup = ''
                try:
                    github.delete_bottle_draft(registration, version, draft)
                except Exception as cleanup_error:
                    cleanup = f'; own draft {draft} retained: {cleanup_error}'
                if isinstance(error, ReleaseTagTaken):
                    return bottle_failed(registration, snapshot, 'bottle-race',
                        f'{error}; no formula written, retry selects and checks the published set{cleanup}')
                return bottle_failed(registration, snapshot, 'destination-unavailable', str(error) + cleanup)
        # Step 7: public URL read-back of every asset before any formula write.
        try:
            final = bottle_rules.select_bottles(registration, snapshot, github)
        except bottle_rules.BottleConflict as error:
            return bottle_failed(registration, snapshot, 'bottle-conflict', 'public read-back: ' + str(error))
        except Exception as error:
            return bottle_failed(registration, snapshot, 'destination-unavailable',
                                 'public read-back unavailable; retry reuses the release: ' + str(error))
        if final is None or final['bottle_set'] != bottles:
            return bottle_failed(registration, snapshot, 'bottle-conflict',
                                 'public read-back differs from the published bottle set')
        return final['bottle_set']
    except Exception as error:
        return bottle_failed(registration, snapshot, 'destination-unavailable', str(error))


def authority(registration, request, github, credentials):
    admission = admit(registration, github, 'authority')
    mode = admission['mode']
    if mode == 'inspect-baseline':
        candidate = inspect_identity(registration, request['tag'], github)
        return dict(state='inspected', admission=admission, candidate=candidate,
                    identity_digest=canonical_digest(candidate))
    if mode == 'approve-baseline':
        candidate = request['candidate']
        fresh = inspect_identity(registration, candidate['tag'], github)
        require(fresh == candidate, 'baseline candidate changed; inspect again')
        approval = dict(owner_id=admission['actor_id'], triggering_actor_id=admission['triggering_actor_id'],
            mode=mode,role='authority',controller_sha=admission['controller_sha'],
            configuration_digest=registration['configuration_digest'], identity_digest=request['approve_sha'],
            reference=approval_reference(admission))
        snapshot = approve_snapshot(registration, fresh, approval)
        credentials.acquire('authority',admission)
        return record_snapshot(registration,snapshot,admission,github)
    if mode in {'retry','status'}:
        version = request['version']
        require(matches(VERSION,version), 'invalid retry version')
        tip = github.branch_tip(CONTROL_REPOSITORY, RECORDS_BRANCH)
        path = f".distribution/releases/{registration['configuration']['app']}/{version}.json"
        entry = github.file(CONTROL_REPOSITORY, tip, path)
        require(entry['type'] == 'file', 'record must be regular')
        snapshot = strict_json(entry['content'])
        validate_snapshot(registration,snapshot)
        require(snapshot['payload']['identity'] == inspect_identity(registration,'v'+version,github),
                'frozen identity conflict')
        return record_snapshot(registration,snapshot,admission,github)
    require(mode == 'prepare', 'unsupported authority operation')
    c = registration['configuration']
    credentials.prepare_read(admission)
    artifact = github.receipt_artifact(c['repository'], request['run_id'], request['attempt'])
    # Parsing is bounded and inert; verify_release independently checks provenance and exact bytes.
    with zipfile.ZipFile(io.BytesIO(artifact['content'])) as archive:
        require(archive.namelist() == ['receipt.json'], 'unexpected receipt members')
        require(archive.getinfo('receipt.json').file_size <= 1024*1024, 'receipt size exceeded')
        receipt = strict_json(archive.read('receipt.json'))
    require(receipt['run_id'] == request['run_id'] and receipt['attempt'] == request['attempt'],
            'requested source run mismatch')
    snapshot = verify_release(registration, receipt, github)
    credentials.acquire('authority', admission)
    return record_snapshot(registration, snapshot, admission, github)


def approval_reference(admission):
    return (f"https://github.com/{CONTROL_REPOSITORY}/actions/runs/"
            f"{admission['run_id']}/attempts/{admission['attempt']}")


def admit(registration, github, role):
    context = github.controller_context()
    require(context['role'] == role, 'wrong isolated job role')
    require(github.environment() == {'authority': 'package-authority', 'publish': 'package-publisher'}[role],
            'missing or wrong branch-restricted environment')
    return authorize_controller(registration, context, github)


def record_snapshot(registration, snapshot, admission, github):
    c = registration['configuration']
    identity = snapshot['payload']['identity']
    path = f".distribution/releases/{c['app']}/{identity['version']}.json"
    for _ in range(5):
        tip = github.branch_tip(CONTROL_REPOSITORY, RECORDS_BRANCH)
        require(tip is not None, 'protected records branch unavailable')
        prior = regular(github.tree(CONTROL_REPOSITORY, tip, paths=[path]), path)
        if prior:
            original = strict_json(prior)
            validate_snapshot(registration, original)
            if original['payload']['identity'] != identity:
                raise IdentityConflict('frozen identity conflict')
            snapshot = original  # Original approval and configuration audit are create-only.
            commit = tip
        else:
            commit = github.cas(CONTROL_REPOSITORY, RECORDS_BRANCH, tip, {path: encode(snapshot)})
            if not commit:
                continue
        read_back = github.file(CONTROL_REPOSITORY, commit, path)
        require(read_back['type'] == 'file' and strict_json(read_back['content']) == snapshot,
                'protected record read-back mismatch')
        return dict(state='recorded', admission=admission, record_ref=dict(schema_version=1,
            app=c['app'], version=identity['version'], controller_sha=admission['controller_sha'],
            configuration_digest=registration['configuration_digest'], records_commit=commit,
            snapshot_digest=snapshot['snapshot_digest']))
    raise ValueError('records cas-exhausted; dispatch a fresh retry')


def apply_channel(channel, registration, snapshot, candidate, github, bottles=None):
    """Apply a candidate after protected admission. This function never mints credentials.

    Brew applies only a bottled formula whose bottle_set apply_bottles published.
    """
    registration = validate_registration(registration)
    validate_snapshot(registration, snapshot)
    c = registration['configuration']
    identity = snapshot['payload']['identity']
    outcome = result(channel, registration, snapshot, definition_digest=file_digest(candidate))
    if channel == 'brew' and bottles is None:
        return {**outcome, 'state': 'failed', 'failure_code': 'bottles-required',
                'message': 'Brew publishes only a bottled formula after its bottles are published'}
    validation = validate_candidate(channel, registration, snapshot, candidate, bottles)
    outcome['validation'] = validation
    if not validation['passed']:
        return {**outcome, 'state': 'failed', 'failure_code': validation['failure_code'],
                'message': '; '.join(validation['errors'])}
    bottle_fields = {}
    if bottles is not None:
        bottle_fields = dict(release_tag=bottles['release_tag'], bottle_set_sha256=canonical_digest(bottles),
                             bottles=dict(bottles['tags']))
        outcome.update(bottle_fields)
    require(validation['candidate_digest'] == file_digest(candidate), 'candidate changed after validation')
    require(set(candidate) == set(expected_paths(channel, c, identity)), 'unregistered path')
    if channel == 'winget':
        return apply_winget(registration,snapshot,candidate,outcome,github)
    destination = c['destinations'][channel]
    repository = destination['repository']
    branch = github.default_branch(repository)
    marker_path = '.distribution/channels/' + c['app'] + '.json'
    marker = dict(schema_version=1, app=c['app'], version=identity['version'],
                  snapshot_digest=snapshot['snapshot_digest'], definition_digest=file_digest(candidate),
                  configuration_digest=registration['configuration_digest'], **bottle_fields)
    for _ in range(5):
        tip = github.branch_tip(repository, branch)
        require(tip is not None, 'destination branch unavailable')
        tree = github.tree(repository, tip, paths=[*candidate, marker_path])
        prior = regular(tree, marker_path)
        if prior:
            previous = strict_json(prior)
            verify_marker(channel,registration,snapshot,previous,tree,github)
            if version_key(previous['version']) > version_key(identity['version']):
                return {**outcome, 'state': 'skipped', 'message': 'newer stable definition retained',
                        'destination_commit': tip}
            if previous['version'] == identity['version']:
                if previous['snapshot_digest'] != snapshot['snapshot_digest']:
                    raise IdentityConflict('same-version identity conflict')
        existing = {path: regular(tree, path) for path in candidate}
        check_evidence(channel,registration,snapshot,candidate,validation,bottles)
        if existing == candidate and prior == encode(marker):
            return {**outcome, 'state': 'published', 'destination_commit': tip,
                    'message': 'identical destination reconciled'}
        commit = github.cas(repository, branch, tip, {**candidate, marker_path: encode(marker)})
        if commit:
            return {**outcome, 'state': 'published', 'destination_commit': commit}
    return {**outcome, 'state': 'failed', 'failure_code': 'cas-exhausted',
            'message': 'five conflicting tips; dispatch a fresh retry'}


def check_evidence(channel,registration,snapshot,candidate,evidence,bottles=None):
    from .validation import validation_environment
    pins = validation_environment()[2]
    require(evidence.get('passed') is True and evidence.get('state') == 'validated'
            and evidence.get('channel') == channel
            and evidence.get('bottle_set_sha256') == (canonical_digest(bottles) if bottles is not None else None)
            and evidence.get('snapshot_digest') == snapshot['snapshot_digest']
            and evidence.get('configuration_digest') == registration['configuration_digest']
            and evidence.get('candidate_digest') == file_digest(candidate), 'candidate/evidence binding changed')
    require(all(evidence['validation'].get(k) == pins[k] for k in
                ['pins_sha256','requirements_sha256','schemas','dependencies']), 'validation pins changed')


def verify_marker(channel,registration,snapshot,marker,tree,github):
    names = 'schema_version app version snapshot_digest definition_digest configuration_digest'
    if channel == 'brew' and type(marker) is dict and 'release_tag' in marker:
        fields(marker,names+' release_tag bottle_set_sha256 bottles')
        require(marker['release_tag'] == f"{registration['configuration']['app']}-{marker['version']}"
                and matches(DIGEST,marker['bottle_set_sha256']) and type(marker['bottles']) is dict
                and all(matches(DIGEST,v) for v in marker['bottles'].values()), 'invalid bottle marker')
    else:
        fields(marker,names)
    c = registration['configuration']
    require(type(marker['schema_version']) is int and marker['schema_version'] == 1
            and marker['app'] == c['app'], 'invalid channel marker')
    version_key(marker['version'])
    if marker['version'] == snapshot['payload']['identity']['version']:
        protected = snapshot
    else:
        try:
            tip = github.branch_tip(CONTROL_REPOSITORY,RECORDS_BRANCH)
            require(tip is not None, 'records unavailable')
            path = f".distribution/releases/{c['app']}/{marker['version']}.json"
            entry = github.file(CONTROL_REPOSITORY,tip,path)
            require(entry['type'] == 'file', 'unsafe record')
            protected = strict_json(entry['content'])
        except Exception as error:
            raise ValueError('channel marker has no readable protected release record') from error
    validate_snapshot(registration,protected)
    if marker['snapshot_digest'] != protected['snapshot_digest']:
        raise IdentityConflict('marker/protected snapshot conflict')
    historical = registration
    if marker['configuration_digest'] != registration['configuration_digest']:
        control = github.controller_context()['sha']
        revision = github.registration_revision(c['app'],marker['configuration_digest'],control)
        require(github.is_ancestor(CONTROL_REPOSITORY,revision['commit'],control) is True,
                'marker registration outside protected history')
        historical = validate_registration(revision['registration'])
        require(historical['configuration_digest'] == marker['configuration_digest'], 'marker config mismatch')
    paths = expected_paths(channel,c,protected['payload']['identity'])
    installed = {path:regular(tree,path) for path in paths}
    require(all(type(b) is bytes for b in installed.values()) and file_digest(installed) == marker['definition_digest'],
            'marker/definition digest conflict')
    # This is a consistency observation, not validation by the current renderer.
    # Existing bytes may come from earlier reviewed code/style. New writes still
    # require full current candidate validation and protected source admission.
    return dict(kind='prior-marker-binding',snapshot_digest=protected['snapshot_digest'],
        configuration_digest=historical['configuration_digest'],definition_digest=file_digest(installed))


def apply_winget(registration,snapshot,candidate,outcome,github):
    c = registration['configuration']
    version = snapshot['payload']['identity']['version']
    observed = observe_winget(registration,version,github,github.source)
    if observed['state'] in {'unknown','submitted','accepted-awaiting-indexing','published'}:
        retained = {**outcome,**observed,'validation':None,'definition_digest':None}
        submitted = observed['refs'].get('submitted_head')
        if submitted and observed['state'] != 'unknown':
            try:
                repo = c['destinations']['winget']['repository']
                submitted_tree = github.tree(repo,submitted,paths=list(candidate))
                submitted_bytes = {p:regular(submitted_tree,p) for p in candidate}
                require(all(type(b) is bytes for b in submitted_bytes.values()), 'submitted files unavailable')
                retained['definition_digest'] = file_digest(submitted_bytes)
                current = observed['refs'].get('current_head')
                if current:
                    tree = github.tree(repo,current,paths=list(candidate))
                    actual = {p:regular(tree,p) for p in candidate}
                    require(all(type(b) is bytes for b in actual.values()), 'current branch files unavailable')
                    retained['refs']['current_definition_digest'] = file_digest(actual)
            except Exception as error:
                if observed['state'] in {'published','accepted-awaiting-indexing'}:
                    retained['definition_observation_error']=str(error)
                    return retained  # Verified source observation survives fork object retention.
                return {**retained,'state':'unknown','failure_code':'observation-unavailable','message':str(error)}
        return retained
    if observed['state'] == 'rejected' and github.controller_context()['mode'] != 'retry':
        return {**outcome,**observed}
    if observed.get('closed_retry') and github.controller_context()['mode'] != 'retry':
        return winget_status(registration,snapshot,github)  # Observe; never rewrite closed work automatically.
    repository = c['destinations']['winget']['repository']
    branch = c['app']+'/'+version
    upstream = github.upstream()
    refs = dict(observed['refs'],upstream_branch=upstream['branch'],upstream_tip=upstream['sha'],branch=branch)
    comparison = (f"https://github.com/microsoft/winget-pkgs/compare/{upstream['branch']}..."
                  f"darkquasar:winget-pkgs:{branch}?expand=1")
    refs['compare_url'] = comparison
    if observed['state'] == 'rejected' and refs.get('submitted_head'):
        try:
            previous = github.tree(repository,refs['submitted_head'],paths=list(candidate))
            previous_bytes = {p:regular(previous,p) for p in candidate}
            require(all(type(b) is bytes for b in previous_bytes.values()), 'prior submitted files unavailable')
            refs['previous_definition_digest'] = file_digest(previous_bytes)
        except Exception as error:
            refs['previous_definition_observation_error'] = str(error)
    for _ in range(5):
        head = github.branch_tip(repository,branch)
        try:
            if not github.commit_available(repository,upstream['sha']):
                raise ValueError('upstream commit unavailable in fork')
            base = github.merge_base(upstream['sha'],repository,head) if head else upstream['sha']
            refs.update(upstream_base=base,prior_head=head)
            if head:
                require(github.diff_paths(repository,base,head) <= set(candidate),
                        'WinGet full branch diff contains unregistered paths; owner recovery required')
            tree = github.tree(repository,head or base,paths=list(candidate))
            existing = {path:regular(tree,path) for path in candidate}
            old_bytes = {p:b for p,b in existing.items() if b is not None}
            check_evidence('winget',registration,snapshot,candidate,outcome['validation'])
            if head and existing == candidate:
                return {**outcome,'state':'prepared','destination_commit':head,
                        'refs':dict(refs,head=head,controller_prepared_head=head),'message':'validated branch reconciled; submit in browser'}
            if old_bytes and 'previous_definition_digest' not in refs:
                # Canonical validation of the correction already passed against frozen binaries.
                refs['previous_definition_digest'] = file_digest(old_bytes)
            commit = github.cas(repository,branch,head,candidate,base=base)
            if not commit:
                continue
            require(github.diff_paths(repository,base,commit) <= set(candidate), 'unexpected prepared branch diff')
            return {**outcome,'state':'prepared','destination_commit':commit,
                    'refs':dict(refs,head=commit,controller_prepared_head=commit),'message':'Validated branch; owner submits in browser'}
        except Exception as error:
            from .github import APIError,RefOutcomeUnknown
            if isinstance(error,RefOutcomeUnknown):
                return {**outcome,'state':'unknown','failure_code':'write-outcome-unknown',
                        'refs':dict(refs,**error.refs),'message':str(error)}
            ref_denied = (isinstance(error,APIError) and error.status in {403,422}
                          and '/git/refs' in error.operation)
            if ref_denied or str(error) == 'upstream commit unavailable in fork':
                return {**outcome,'state':'failed','failure_code':'fork-sync-required','refs':refs,
                    'message':f"Fork operation denied/unavailable: {error}. Owner: gh repo sync darkquasar/winget-pkgs --branch {upstream['branch']} --source microsoft/winget-pkgs; retry only winget without force or wider credentials"}
            if isinstance(error,APIError):
                return {**outcome,'state':'failed','failure_code':'destination-unavailable','refs':refs,'message':str(error)}
            raise
    return {**outcome,'state':'failed','failure_code':'cas-exhausted','refs':refs,
            'message':'five conflicting branch tips; dispatch fresh retry'}
