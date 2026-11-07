"""Bounded GitHub REST adapter. Production credentials are acquired only after admission."""
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import CONTROL_BRANCH, CONTROL_REPOSITORY, OWNER_ID, RECORDS_BRANCH, SHA, VERSION, fields, matches, positive_id, require, validate_registration
from .snapshot import strict_json


def safe_path(path):
    require(type(path) is str and path and not path.startswith('/')
            and all(part not in {'', '.', '..'} for part in path.split('/'))
            and all(ord(ch) >= 32 for ch in path) and '\\' not in path, 'unsafe repository path')
    return path


class GitHub:
    def __init__(self, *, context=None, environment=None, request=None):
        self.context = context
        self.environment_name = environment
        self.transport = Transport(read_token=os.environ.get('GITHUB_TOKEN')) if request is None else None
        self.request = request or self.transport
        self.write_role = None
        self.configuration = None
        self.source = WindowsSource(self)

    def run(self, repository, run_id, attempt):
        raw = self.request('GET', f'/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}')
        return dict(id=raw['id'], attempt=raw['run_attempt'], repository=raw['repository']['full_name'],
            workflow_path=raw['path'], head_sha=raw['head_sha'], head_branch=raw['head_branch'],
            event=raw['event'], actor_id=raw['actor']['id'], triggering_actor_id=raw['triggering_actor']['id'],
            status=raw['status'], conclusion=raw['conclusion'])

    def tree(self, repository, commit, paths=None):
        require(matches(SHA, commit) and paths is not None, 'immutable commit and bounded paths required')
        root = self.request('GET', f'/repos/{repository}/git/commits/{commit}')['tree']['sha']
        cache = {}
        def entries(sha):
            if sha not in cache:
                raw = self.request('GET', f'/repos/{repository}/git/trees/{sha}')
                require(not raw.get('truncated', False), 'incomplete Git tree')
                cache[sha] = {e['path']:e for e in raw['tree']}
            return cache[sha]
        result = {}
        for path in paths:
            parts = safe_path(path).split('/')
            current = root
            for index, name in enumerate(parts):
                entry = entries(current).get(name)
                if entry is None:
                    break
                if entry['type'] == 'tree' and entry['mode'] == '040000':
                    current = entry['sha']
                    if index == len(parts)-1:
                        result[path] = {'type':'tree','content':b''}
                    continue
                kind = 'file' if entry['type'] == 'blob' and entry['mode'] in {'100644','100755'} else 'symlink'
                key = '/'.join(parts[:index+1])
                data = b''
                if index == len(parts)-1:
                    blob = self.request('GET', f"/repos/{repository}/git/blobs/{entry['sha']}")
                    require(blob['encoding'] == 'base64', 'unsupported blob encoding')
                    data = base64.b64decode(blob['content'])
                    require(len(data) <= 1024*1024, 'repository file exceeds bound')
                result[key] = {'type':kind,'content':data}
                break
        return result

    def ref(self, repository, ref):
        raw = self.request('GET', f'/repos/{repository}/git/ref/{quote(ref, safe="/")}')
        return raw['object']

    def branch_tip(self, repository, branch):
        try:
            value = self.ref(repository, 'heads/'+branch)
        except APIError as error:
            if error.status == 404:
                return None
            raise
        require(value['type'] == 'commit' and matches(SHA,value['sha']), 'invalid branch tip')
        return value['sha']

    def default_branch(self, repository):
        return self.request('GET', f'/repos/{repository}')['default_branch']

    def receipt_artifact(self, repository, run_id, attempt):
        return self.artifact_named(repository,run_id,f'distribution-receipt-{run_id}-{attempt}')

    def artifact_named(self, repository, run_id, name):
        from .contracts import DIGEST
        artifacts = self.pages(f'/repos/{repository}/actions/runs/{run_id}/artifacts', 'artifacts')
        matches_ = [a for a in artifacts if a.get('name') == name]
        require(len(matches_) == 1, 'receipt-unavailable or ambiguous')
        a = matches_[0]
        require(a['expired'] is False and a['workflow_run']['id'] == run_id
                and matches(SHA,a['workflow_run']['head_sha']), 'artifact provenance mismatch')
        require(type(a.get('digest')) is str and a['digest'].startswith('sha256:')
                and matches(DIGEST,a['digest'][7:]), 'artifact API digest unavailable')
        data = self.request('GET', f"/repos/{repository}/actions/artifacts/{a['id']}/zip",
                            raw=True, limit=1024*1024)
        require(hashlib.sha256(data).hexdigest() == a['digest'][7:], 'artifact API digest mismatch')
        return dict(id=a['id'],name=a['name'],run_id=a['workflow_run']['id'],
                    head_sha=a['workflow_run']['head_sha'],expired=a['expired'],
                    sha256=a['digest'][7:],content=data)

    def list_registrations(self, repository, control_sha):
        require(repository == CONTROL_REPOSITORY, 'registry repository denied')
        directory = '.distribution/registrations'
        entries = self.directory(repository,control_sha,directory)
        require(len(entries) <= 100, 'registry size bound exceeded')
        documents = []
        for entry in entries:
            require(entry['mode'] == '100644' and entry['type'] == 'blob'
                    and matches(r'[a-z][a-z0-9-]*\.json',entry['path']), 'unsafe registry entry')
            doc = validate_registration(strict_json(self.file(repository,control_sha,directory+'/'+entry['path'])['content']))
            require(entry['path'] == doc['configuration']['app']+'.json', 'registry filename/app mismatch')
            documents.append(doc)
        return documents

    def directory(self, repository, commit, path):
        require(matches(SHA,commit), 'directory requires immutable commit')
        current = self.request('GET',f'/repos/{repository}/git/commits/{commit}')['tree']['sha']
        for part in safe_path(path).split('/'):
            raw = self.request('GET',f'/repos/{repository}/git/trees/{current}')
            require(not raw.get('truncated',False), 'incomplete directory tree')
            entries = [e for e in raw['tree'] if e['path'] == part]
            if not entries:
                return []
            require(len(entries) == 1 and entries[0]['mode'] == '040000'
                    and entries[0]['type'] == 'tree', 'unsafe directory parent')
            current = entries[0]['sha']
        raw = self.request('GET',f'/repos/{repository}/git/trees/{current}')
        require(not raw.get('truncated',False), 'incomplete directory listing')
        return raw['tree']

    def file(self, repository, commit, path):
        entries = self.tree(repository,commit,paths=[path])
        require(path in entries, 'file unavailable or unsafe parent')
        return entries[path]

    def repository(self, repository):
        raw = self.request('GET',f'/repos/{repository}')
        return dict(full_name=raw['full_name'],owner_id=raw['owner']['id'])

    def tag(self, repository, sha):
        return self.request('GET',f'/repos/{repository}/git/tags/{sha}')['object']

    def is_ancestor(self, repository, ancestor, tip):
        require(matches(SHA,ancestor) and matches(SHA,tip), 'ancestry requires immutable commits')
        raw = self.request('GET',f'/repos/{repository}/compare/{ancestor}...{tip}')
        return raw['status'] in {'ahead','identical'} and raw['merge_base_commit']['sha'] == ancestor

    def user(self, actor_id):
        return self.request('GET',f'/user/{actor_id}')

    def app(self, slug):
        return self.request('GET','/apps/'+quote(slug,safe=''))

    def controller_context(self):
        require(type(self.context) is dict, 'workflow context unavailable')
        return dict(self.context)

    def environment(self):
        return self.environment_name

    def registration_revision(self, app, digest, current_control_sha):
        require(matches(r'[a-z][a-z0-9-]*',app), 'invalid app')
        path = f'.distribution/registrations/{app}.json'
        commits = self.pages(f'/repos/{CONTROL_REPOSITORY}/commits?sha={current_control_sha}&path={quote(path,safe="")}')
        for entry in commits:
            document = validate_registration(strict_json(self.file(CONTROL_REPOSITORY,entry['sha'],path)['content']))
            if document['configuration_digest'] == digest:
                return dict(commit=entry['sha'],registration=document)
        raise ValueError('initial protected registration unavailable')

    def release(self, repository, tag):
        raw = self.request('GET',f'/repos/{repository}/releases/tags/{quote(tag,safe="")}')
        return dict(tag_name=raw['tag_name'],draft=raw['draft'],prerelease=raw['prerelease'],
            assets=self.pages(f"/repos/{repository}/releases/{raw['id']}/assets"))

    def download_asset(self, repository, tag, name):
        require(matches(r'darkquasar/[A-Za-z0-9_.-]+',repository)
                and matches('v'+VERSION,tag) and matches(r'[A-Za-z0-9][A-Za-z0-9_.-]*',name)
                and '..' not in name, 'unapproved release coordinates')
        limit = 1024*1024 if name == 'checksums.txt' else 256*1024*1024
        return self.request('GET',f'https://github.com/{repository}/releases/download/{tag}/{name}',
                            raw=True,limit=limit,delivery='release')

    def diff_paths(self, repository, base, head):
        require(matches(SHA,base) and matches(SHA,head), 'diff requires immutable commits')
        roots = [self.request('GET',f'/repos/{repository}/git/commits/{c}')['tree']['sha'] for c in [base,head]]
        cache = {}
        changed = set()
        visited = 0
        def entries(sha):
            if sha is None:
                return {}
            if sha not in cache:
                raw = self.request('GET',f'/repos/{repository}/git/trees/{sha}')
                require(not raw.get('truncated',False), 'incomplete full-diff tree')
                cache[sha] = {e['path']:e for e in raw['tree']}
            return cache[sha]
        def walk(old,new,prefix):
            nonlocal visited
            visited += 1
            require(visited <= 1000 and len(changed) <= 1000, 'full diff bound exceeded')
            if old == new:
                return
            left,right = entries(old),entries(new)
            for name in sorted(set(left)|set(right)):
                a,b = left.get(name),right.get(name)
                if a == b:
                    continue
                path = prefix+name
                a_tree = a is not None and a['type'] == 'tree'
                b_tree = b is not None and b['type'] == 'tree'
                if a_tree or b_tree:
                    if a is not None and not a_tree or b is not None and not b_tree:
                        changed.add(path)
                    walk(a['sha'] if a_tree else None,b['sha'] if b_tree else None,path+'/')
                else:
                    changed.add(path)
        walk(*roots,'')
        require(len(changed) <= 1000, 'full diff bound exceeded')
        return changed

    def upstream(self):
        repo = 'microsoft/winget-pkgs'
        branch = self.default_branch(repo)
        sha = self.branch_tip(repo,branch)
        require(sha is not None, 'Microsoft default branch unavailable')
        return dict(repository=repo,branch=branch,sha=sha)

    def commit_available(self, repository, commit):
        try:
            value = self.request('GET',f'/repos/{repository}/git/commits/{commit}')
            return value.get('sha') == commit
        except APIError as error:
            if error.status == 404:
                return False
            raise

    def merge_base(self, upstream, repository, head):
        require(matches(SHA,upstream) and matches(SHA,head), 'merge-base requires immutable commits')
        raw = self.request('GET',f'/repos/{repository}/compare/{upstream}...{head}')
        base = raw['merge_base_commit']['sha']
        require(matches(SHA,base), 'merge base unavailable')
        return base

    def pull_requests(self, registration, version):
        from .validation import expected_paths
        c = validate_registration(registration)['configuration']
        require(matches(VERSION,version), 'invalid PR version')
        branch = c['app']+'/'+version
        path = '/repos/microsoft/winget-pkgs'
        query = ('?state=all&head='+quote('darkquasar:'+branch,safe='')
                 +'&base='+quote(self.default_branch('microsoft/winget-pkgs'),safe=''))
        listing = self.pages(path+'/pulls'+query)
        try:
            prior = self.status_cache(registration,version).get('channels',{}).get('winget',{})
            retained = prior.get('refs',{}).get('pull_requests',[])
            require(type(retained) is list and all(type(p) is dict and positive_id(p.get('number')) for p in retained),
                    'malformed cached PR references')
        except Exception:
            retained = []  # The live exact-head lookup above remains mandatory and complete.
        numbers = {p['number'] for p in listing}
        numbers.update(p['number'] for p in retained)
        require(len(numbers) <= 100, 'PR history bound exceeded')
        result = []
        allowed = set(expected_paths('winget',c,{'version':version}))
        for number in sorted(numbers):
            require(type(number) is int and number > 0, 'invalid retained PR reference')
            pr = self.request('GET',path+f'/pulls/{number}')
            require(pr['head']['repo'] is not None
                    and pr['head']['repo']['full_name'] == c['destinations']['winget']['repository']
                    and pr['head']['ref'] == branch and pr['base']['repo']['full_name'] == 'microsoft/winget-pkgs',
                    'PR does not match exact registered fork/head')
            files = self.pages(path+f'/pulls/{number}/files')
            require(files and {f['filename'] for f in files} <= allowed, 'PR changes outside registered version')
            feedback = []
            if pr['state'] == 'closed' and not pr['merged']:
                comments = self.pages(path+f'/issues/{number}/comments')
                reviews = self.pages(path+f'/pulls/{number}/reviews')
                feedback = [item.get('body','') for item in comments+reviews]
            result.append(dict(number=number,url=pr['html_url'],state=pr['state'],merged=pr['merged'],
                head_sha=pr['head']['sha'],head_repository=pr['head']['repo']['full_name'],head_branch=branch,
                feedback='\n'.join(feedback)[-8192:]))
        return result

    def status_cache(self, registration, version):
        from .state import regular
        c = validate_registration(registration)['configuration']
        require(matches(VERSION,version), 'invalid status version')
        branch = self.default_branch(CONTROL_REPOSITORY)
        require(branch not in {CONTROL_BRANCH,RECORDS_BRANCH}, 'native status branch not commissioned')
        tip = self.branch_tip(CONTROL_REPOSITORY,branch)
        path = f".distribution/status/{c['app']}/{version}.json"
        raw = regular(self.tree(CONTROL_REPOSITORY,tip,paths=[path]),path)
        return strict_json(raw) if raw else {}

    def release_versions(self, registration):
        app = validate_registration(registration)['configuration']['app']
        tip = self.branch_tip(CONTROL_REPOSITORY,RECORDS_BRANCH)
        entries = self.directory(CONTROL_REPOSITORY,tip,'.distribution/releases/'+app)
        require(len(entries) <= 1000, 'release history bound exceeded')
        versions = []
        for entry in entries:
            require(entry['type'] == 'blob' and entry['mode'] == '100644'
                    and matches(VERSION+r'\.json',entry['path']), 'unsafe release history entry')
            versions.append(entry['path'][:-5])
        return versions

    def pages(self, path, key=None):
        result = []
        separator = '&' if '?' in path else '?'
        for page in range(1,101):
            raw = self.request('GET', f'{path}{separator}per_page=100&page={page}')
            items = raw[key] if key else raw
            require(type(items) is list, 'incomplete API listing')
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError('API pagination bound exceeded; observation unknown')

    def cas(self, repository, branch, expected, changes, *, base=None):
        c = self.configuration
        require(c is not None and self.write_role in {'authority','publish'}, 'no admitted write scope')
        for path, data in changes.items():
            safe_path(path)
            require(type(data) is bytes and 0 < len(data) <= 1024*1024, 'invalid write bytes')
        if self.write_role == 'authority':
            require(repository == CONTROL_REPOSITORY and branch == RECORDS_BRANCH and len(changes) == 1,
                    'authority destination denied')
            require(all(matches(re.escape('.distribution/releases/'+c['app']+'/')+VERSION+r'\.json',p)
                        for p in changes), 'authority path denied')
        else:
            require(branch not in {CONTROL_BRANCH, RECORDS_BRANCH}, 'protected branch denied')
            allowed = set()
            if repository == CONTROL_REPOSITORY:
                allowed.add(c['destinations']['brew']['path'])
                allowed.add('.distribution/channels/'+c['app']+'.json')
                allowed.update(p for p in changes if matches(re.escape('.distribution/status/'+c['app']+'/')+VERSION+r'\.json',p))
            if repository == c['destinations']['scoop']['repository']:
                allowed.update({c['destinations']['scoop']['path'], '.distribution/channels/'+c['app']+'.json'})
            if repository == c['destinations']['winget']['repository']:
                require(matches(re.escape(c['app']+'/')+VERSION, branch), 'WinGet branch denied')
                version = branch.split('/')[1]
                prefix = c['destinations']['winget']['path']+'/'+version+'/'+c['identifiers']['winget']
                allowed.update(prefix+s+'.yaml' for s in ['', '.installer', '.locale.en-US'])
            else:
                require(branch == self.default_branch(repository), 'native branch denied')
            require(changes and set(changes) <= allowed, 'write outside registered paths')
        if self.branch_tip(repository,branch) != expected:
            return None
        parent = expected or base
        require(matches(SHA,parent), 'CAS requires immutable base')
        existing = self.tree(repository,parent,paths=list(changes))
        from .state import regular
        for path in changes:
            prior = regular(existing,path)
            require(self.write_role != 'authority' or prior is None, 'record overwrite denied')
        root = self.request('GET', f'/repos/{repository}/git/commits/{parent}')['tree']['sha']
        entries = []
        for path, data in sorted(changes.items()):
            blob = self.request('POST', f'/repos/{repository}/git/blobs',
                                {'content':base64.b64encode(data).decode(),'encoding':'base64'})
            entries.append(dict(path=path,mode='100644',type='blob',sha=blob['sha']))
        tree = self.request('POST', f'/repos/{repository}/git/trees', {'base_tree':root,'tree':entries})
        commit = self.request('POST', f'/repos/{repository}/git/commits',
            {'message':'Update registered package distribution','tree':tree['sha'],'parents':[parent]})['sha']
        try:
            if expected is None:
                self.request('POST', f'/repos/{repository}/git/refs', {'ref':'refs/heads/'+branch,'sha':commit})
            else:
                self.request('PATCH', f'/repos/{repository}/git/refs/heads/{quote(branch,safe="/")}',
                             {'sha':commit,'force':False})
        except Exception as error:
            if isinstance(error,APIError) and error.status < 500:
                if error.status in {409,422} and self.branch_tip(repository,branch) != expected:
                    return None
                raise
            try:
                observed = self.branch_tip(repository,branch)
            except Exception:
                observed = None
            if observed == commit:
                return commit
            raise RefOutcomeUnknown(repository,branch,commit) from error
        return commit


class APIError(ValueError):
    def __init__(self, status, operation):
        self.status = status
        self.operation = operation
        super().__init__(f'GitHub HTTP {status}: {operation}')


class RefOutcomeUnknown(ValueError):
    def __init__(self,repository,branch,commit):
        self.refs=dict(repository=repository,branch=branch,candidate_commit=commit)
        super().__init__(f'Ref outcome unknown: {repository} {branch} candidate {commit}; read back before fresh retry')


class Transport:
    def __init__(self, read_token=None, opener=None):
        self.read_token = read_token
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = opener or build_opener(NoRedirect)
        self.write_tokens = {}
        self.read_tokens = {}

    def __call__(self, method, path, body=None, *, raw=False, limit=8*1024*1024,
                 delivery=None, token=None):
        require(method in {'GET','POST','PATCH','DELETE'}, 'unsupported API method')
        initial = 'https://api.github.com'+path if path.startswith('/') else path
        parsed = urlparse(initial)
        require(parsed.scheme == 'https' and parsed.port in {None,443} and not parsed.username
                and not parsed.password and not parsed.fragment, 'unsafe API URL')
        if delivery == 'release':
            require(parsed.hostname == 'github.com' and method == 'GET'
                    and matches(r'/darkquasar/[A-Za-z0-9_.-]+/releases/download/v'+VERSION+r'/[A-Za-z0-9_.-]+',parsed.path),
                    'unapproved release URL')
        else:
            require(parsed.hostname == 'api.github.com' and path.startswith('/'), 'arbitrary API URL denied')
        if token is None:
            token = self._read_token(path) if method == 'GET' else self._write_token(path)
        data = json.dumps(body,separators=(',',':')).encode() if body is not None else None
        attempts = 4 if method == 'GET' else 1
        for attempt in range(attempts):
            url = initial
            deadline = time.monotonic()+30
            try:
                for _ in range(6):
                    parsed = urlparse(url)
                    host = parsed.hostname
                    require(parsed.scheme == 'https' and parsed.port in {None,443}
                            and not parsed.username and not parsed.password, 'unsafe redirect URL')
                    if delivery == 'release':
                        require(host in {'github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'},
                                'unapproved release redirect host')
                    else:
                        require(host in {'api.github.com','results-receiver.actions.githubusercontent.com'}
                                or re.fullmatch(r'productionresultssa[0-9]+\.blob\.core\.windows\.net',host or ''),
                                'unapproved artifact redirect host')
                    headers = {'Accept':'application/octet-stream' if raw else 'application/vnd.github+json',
                               'User-Agent':'patronus-distribution','X-GitHub-Api-Version':'2022-11-28'}
                    if host == 'api.github.com' and token:
                        headers['Authorization'] = 'Bearer '+token
                    if data is not None:
                        headers['Content-Type'] = 'application/json'
                    remaining = deadline-time.monotonic()
                    require(remaining > 0, 'request time bound exceeded')
                    request = Request(url,data=data,headers=headers,method=method)
                    try:
                        response = self.opener.open(request,timeout=remaining)
                    except HTTPError as error:
                        if error.code in {301,302,303,307,308} and method == 'GET':
                            url = error.headers.get('Location','')
                            continue
                        raise
                    with response:
                        status = getattr(response,'status',200)
                        chunks = []
                        count = 0
                        while True:
                            require(time.monotonic() < deadline, 'request time bound exceeded')
                            chunk = response.read(min(65536,limit-count+1))
                            if not chunk:
                                break
                            count += len(chunk)
                            require(count <= limit, 'download size bound exceeded')
                            chunks.append(chunk)
                        content = b''.join(chunks)
                    if status == 204:
                        require(not content, 'unexpected body in no-content response')
                        return None
                    return content if raw else strict_json(content)
                raise ValueError('redirect bound exceeded')
            except HTTPError as error:
                if method != 'GET' or error.code not in {429,500,502,503,504} or attempt == attempts-1:
                    raise APIError(error.code,method+' '+urlparse(initial).path) from None
            except (URLError, TimeoutError, OSError):
                if attempt == attempts-1:
                    raise ValueError('GitHub request unavailable; external outcome unknown') from None
            time.sleep(min(2**attempt,4))

    def _write_token(self,path):
        parts = path.split('/')
        repository = '/'.join(parts[2:4]) if len(parts) >= 4 and parts[1] == 'repos' else None
        record = self.write_tokens.get(repository)
        require(record is not None and record['expires_at'] > time.time(), 'credential-expired or unavailable')
        return record['token']

    def _read_token(self,path):
        parts=path.split('/')
        repository='/'.join(parts[2:4]) if len(parts) >= 4 and parts[1] == 'repos' else None
        record=self.read_tokens.get(repository)
        if record is None:
            return self.read_token
        require(record['expires_at'] > time.time(), 'producer read credential expired')
        return record['token']


class AppCredentials:
    def __init__(self, github, registration, env=None, signer=None):
        self.github = github
        self.registration = registration
        self.env = os.environ if env is None else env
        self.signer = signer or sign_jwt
        self.tokens = []
        self.writer_acquired = False
        self.writer_repositories = set()
        self.reader_acquired = False

    def acquire(self, role, admission, *, repository=None):
        from .controller import admit
        c = self.registration['configuration']
        if role == 'publish':
            repository = repository or CONTROL_REPOSITORY
            require(repository in {d['repository'] for d in c['destinations'].values()}, 'unregistered token repository')
            if repository in self.writer_repositories:
                record = self.github.transport.write_tokens.get(repository)
                require(record is not None and record['expires_at'] > time.time(),
                        'repository credential unavailable/expired; dispatch fresh retry')
                return
        else:
            require(not self.writer_acquired and repository in {None,CONTROL_REPOSITORY}, 'no token refresh through stale code')
        fresh = admit(self.registration,self.github,role)
        require(fresh == admission, 'credential admission changed')
        other = 'PUBLISH' if role == 'authority' else 'AUTHORITY'
        require(not self.env.get('PACKAGE_'+other+'_APP_PRIVATE_KEY'), 'opposite role key in isolated runner')
        prefix = 'PACKAGE_'+('AUTHORITY' if role == 'authority' else 'PUBLISH')+'_APP_'
        repositories = [CONTROL_REPOSITORY] if role == 'authority' else [repository]
        self.writer_acquired = True
        if role == 'publish':
            self.writer_repositories.add(repository)
        record = self._mint(prefix,repositories,{'contents':'write'})
        for repository in repositories:
            self.github.transport.write_tokens[repository] = record
        self.github.write_role = role
        self.github.configuration = c

    def _mint(self,prefix,repositories,requested_permissions):
        app_id = self.env[prefix+'ID']
        require(matches(r'[1-9][0-9]*',app_id), 'App ID unavailable')
        key = self.env[prefix+'PRIVATE_KEY']
        now = int(time.time())
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=')
        header = b64(b'{"alg":"RS256","typ":"JWT"}')
        claims = b64(json.dumps(dict(iat=now-60,exp=now+540,iss=app_id),separators=(',',':')).encode())
        message = header+b'.'+claims
        jwt = (message+b'.'+b64(self.signer(message,key))).decode()
        installations = []
        for repository in repositories:
            installation = self.github.request('GET',f'/repos/{repository}/installation',token=jwt)
            installations.append(installation['id'])
        require(len(set(installations)) == 1, 'App must use the selected repository installation only')
        selected = [repository.split('/')[1] for repository in repositories]
        token = self.github.request('POST',f'/app/installations/{installations[0]}/access_tokens',
            {'repositories':selected,'permissions':requested_permissions},token=jwt)
        # Retain immediately so a malformed/short-lived response is still revoked in finally.
        require(type(token.get('token')) is str and token['token'], 'installation token unavailable')
        self.tokens.append(token['token'])
        expiry = datetime.fromisoformat(token['expires_at'].replace('Z','+00:00')).timestamp()
        require(expiry >= time.time()+20*60, 'credential-expired or lifetime shorter than remaining job bound')
        permissions = token['permissions']
        require(all(permissions.get(k) == v for k,v in requested_permissions.items())
                and all(k in {*requested_permissions,'metadata'} for k in permissions), 'unexpected token permissions')
        require(self.github.transport is not None, 'production transport unavailable')
        return {'token':token['token'],'expires_at':expiry}

    def prepare_read(self,admission):
        from .controller import admit
        require(not self.reader_acquired and admission['mode'] == 'prepare' and admission['role'] == 'authority',
                'source read installation is authority-prepare only')
        require(admit(self.registration,self.github,'authority') == admission, 'source reader admission changed')
        require(not self.env.get('PACKAGE_PUBLISH_APP_PRIVATE_KEY'), 'publisher key in authority runner')
        source_id=self.env.get('SOURCE_READ_APP_ID')
        require(source_id is not None and source_id != self.env.get('PACKAGE_AUTHORITY_APP_ID'),
                'separate producer read-only App required')
        self.reader_acquired = True
        repository=self.registration['configuration']['repository']
        record=self._mint('SOURCE_READ_APP_',[repository],{'actions':'read','contents':'read'})
        self.github.transport.read_tokens[repository]=record

    def close(self):
        errors = []
        for token in self.tokens:
            try:
                self.github.request('DELETE','/installation/token',token=token)
            except Exception:
                errors.append('ephemeral token revocation unavailable; expiry remains bounded')
        self.tokens.clear()
        if self.github.transport:
            self.github.transport.write_tokens.clear()
            self.github.transport.read_tokens.clear()
        self.github.write_role = None
        self.github.configuration = None
        return errors


def source_command(package_id):
    return ['winget','show','--id',package_id,'--exact','--source','winget','--versions',
            '--accept-source-agreements','--disable-interactivity']


def source_versions(package_id, exit_code, stdout):
    """Only a successful version table or Microsoft's exact no-match HRESULT is evidence."""
    require(type(exit_code) is int and type(stdout) is str and len(stdout.encode()) <= 65536,
            'invalid source command result')
    if exit_code & 0xffffffff == 0x8A150014:  # APPINSTALLER_CLI_ERROR_NO_APPLICATIONS_FOUND
        return []
    require(exit_code == 0, 'WinGet source query failed; visibility unknown')
    clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',stdout)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    separators = [i for i,line in enumerate(lines) if re.fullmatch(r'-{3,}',line)]
    require(len(separators) == 1, 'WinGet version table unavailable')
    start = separators[0]
    require(start >= 1 and lines[start-1].split() in [['Version'],['Version','Channel']]
            and any('['+package_id+']' in line for line in lines[:start-1]),
            'WinGet package/table identity unavailable')
    rows = lines[start+1:]
    require(rows and all(matches(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}',row) for row in rows),
            'WinGet version table incomplete or unsupported')
    return list(dict.fromkeys(row for row in rows if matches(VERSION,row)))


def run_source_command(args, timeout):
    """Bound native query time and both output streams without invoking a shell."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr)
        deadline = time.monotonic()+timeout
        try:
            while process.poll() is None:
                require(time.monotonic() < deadline, 'WinGet command timed out')
                require(os.fstat(stdout.fileno()).st_size <= 65536
                        and os.fstat(stderr.fileno()).st_size <= 65536, 'WinGet output exceeds bound')
                try:
                    process.wait(timeout=min(0.1,max(0.001,deadline-time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            stdout.seek(0)
            stderr.seek(0)
            out,err = stdout.read(65537),stderr.read(65537)
            require(len(out) <= 65536 and len(err) <= 65536, 'WinGet output exceeds bound')
            return subprocess.CompletedProcess(args,process.returncode,out,err)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def collect_windows_source(registration, github, *, command=None, runner=None):
    """Read-only T1 admission needs no privileged role environment or App keys."""
    from .trust import authorize_controller
    import platform
    require(not any(k.endswith('_APP_PRIVATE_KEY') and v for k,v in os.environ.items())
            and not os.environ.get('DISTRIBUTION_ENVIRONMENT'), 'privileged key/environment in source runner')
    context = github.controller_context()
    require(context['role'] == 'authority' and context['mode'] in {'prepare','approve-baseline','retry','status'},
            'source query requires an eligible admitted request')
    authorize_controller(registration,context,github)
    runner = runner or dict(os=platform.system(),version=platform.version(),architecture=platform.machine(),
        image_os=os.environ.get('ImageOS',''),image_version=os.environ.get('ImageVersion',''))
    fields(runner,'os version architecture image_os image_version')
    require(runner['os'] == 'Windows' and all(type(v) is str and v for v in runner.values()),
            'Windows runner identity unavailable')
    jobs = github.pages(f'/repos/{CONTROL_REPOSITORY}/actions/runs/{context["run_id"]}'
                        f'/attempts/{context["attempt"]}/jobs','jobs')
    jobs = [job for job in jobs if job['name'] == 'winget-source']
    require(len(jobs) == 1 and positive_id(jobs[0]['id']) and jobs[0]['head_sha'] == context['sha']
            and jobs[0]['status'] == 'in_progress'
            and any(label.lower().startswith('windows') for label in jobs[0]['labels']),
            'current Windows query job unavailable')
    command = command or run_source_command
    def execute(args,timeout):
        value = command(args,timeout)
        require(type(value.returncode) is int and type(value.stdout) is bytes and type(value.stderr) is bytes
                and len(value.stdout) <= 65536 and len(value.stderr) <= 65536,
                'native query result exceeds bound or is invalid')
        return dict(command=args,exit=value.returncode,stdout=value.stdout.decode('utf-8-sig'),
                    stderr=value.stderr.decode('utf-8-sig'))
    c = registration['configuration']
    document = dict(schema_version=2,repository=CONTROL_REPOSITORY,controller_sha=context['sha'],
        run_id=context['run_id'],attempt=context['attempt'],job_id=jobs[0]['id'],app=c['app'],
        package_id=c['identifiers']['winget'],source='winget',runner_os='Windows',runner_version=runner,
        observed_at=datetime.now(timezone.utc).isoformat())
    try:
        client = execute(['winget','--version'],30)
        document.update(manager_probe=client,manager_version=client['stdout'].strip())
        require(client['exit'] == 0 and len(client['stdout'].encode()) <= 128
                and len(client['stderr'].encode()) <= 4096
                and matches(r'v?[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?',client['stdout'].strip()),
                'WinGet client version unavailable')
        query = execute(source_command(c['identifiers']['winget']),180)
        document.update(query)
        document['versions'] = source_versions(c['identifiers']['winget'],query['exit'],query['stdout'])
        return document
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        return dict(document,state='unknown',failure_code='observation-unavailable',message=str(error))


class WindowsSource:
    """Independently verify a completed Windows query job in the current admitted run."""
    def __init__(self,github,locator=None):
        self.github = github
        self.locator = locator

    def observe(self,registration,version):
        import io
        import zipfile
        from .trust import authorize_controller
        context = self.github.controller_context()
        require(context['role'] == 'publish', 'source evidence requires admitted publisher')
        authorize_controller(registration,context,self.github)
        locator = self.locator
        if locator is None:
            locator = dict(run_id=context['run_id'],attempt=context['attempt'])
        fields(locator,'run_id attempt')
        require(positive_id(locator['run_id']) and positive_id(locator['attempt'])
                and locator == dict(run_id=context['run_id'],attempt=context['attempt']),
                'source evidence must belong to the current admitted run/attempt')
        c = validate_registration(registration)['configuration']
        sha = context['sha']
        run_id,attempt = locator['run_id'],locator['attempt']
        artifact = self.github.artifact_named(CONTROL_REPOSITORY,run_id,f'winget-source-{run_id}-{attempt}')
        require(artifact['head_sha'] == sha, 'source artifact revision mismatch')
        with zipfile.ZipFile(io.BytesIO(artifact['content'])) as archive:
            require(archive.namelist() == ['source.json'], 'unexpected source evidence archive')
            info = archive.getinfo('source.json')
            require(info.file_size <= 1024*1024 and (info.external_attr >> 16) & 0o170000 != 0o120000,
                    'unsafe source evidence member')
            observation = strict_json(archive.read(info))
        fields(observation,'schema_version repository controller_sha run_id attempt job_id app package_id source '
               'runner_os runner_version command exit stdout stderr versions observed_at manager_version manager_probe')
        require(type(observation['schema_version']) is int and observation['schema_version'] == 2
                and observation['repository'] == CONTROL_REPOSITORY
                and observation['controller_sha'] == sha and observation['run_id'] == run_id
                and observation['attempt'] == attempt and observation['app'] == c['app']
                and observation['package_id'] == c['identifiers']['winget']
                and observation['source'] == 'winget' and observation['runner_os'] == 'Windows'
                and observation['command'] == source_command(c['identifiers']['winget']),
                'source evidence identity/command mismatch')
        require(type(observation['versions']) is list
                and observation['versions'] == source_versions(c['identifiers']['winget'],
                    observation['exit'],observation['stdout'])
                and type(observation['stderr']) is str and len(observation['stderr'].encode()) <= 65536,
                'source versions differ from retained command result')
        runner = observation['runner_version']
        fields(runner,'os version architecture image_os image_version')
        require(runner['os'] == 'Windows' and all(type(v) is str and v for v in runner.values()),
                'Windows runner version unavailable')
        probe = observation['manager_probe']
        fields(probe,'command exit stdout stderr')
        require(probe['command'] == ['winget','--version'] and type(probe['exit']) is int and probe['exit'] == 0
                and type(probe['stdout']) is str and type(probe['stderr']) is str
                and len(probe['stdout'].encode()) <= 128 and len(probe['stderr'].encode()) <= 4096
                and matches(r'v?[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?',probe['stdout'].strip())
                and probe['stdout'].strip() == observation['manager_version'], 'WinGet client evidence unavailable')
        require(type(observation['observed_at']) is str, 'source observation time unavailable')
        observed = datetime.fromisoformat(observation['observed_at'].replace('Z','+00:00'))
        require(observed.tzinfo is not None and -300 <= (datetime.now(timezone.utc)-observed).total_seconds() <= 3600,
                'source observation is stale or in the future')
        jobs = self.github.pages(f'/repos/{CONTROL_REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs','jobs')
        matching = [j for j in jobs if j['id'] == observation['job_id']]
        require(len(matching) == 1 and matching[0]['name'] == 'winget-source'
                and matching[0]['head_sha'] == sha and matching[0]['status'] == 'completed'
                and matching[0]['conclusion'] == 'success'
                and any(label.lower().startswith('windows') for label in matching[0]['labels']),
                'Windows source job evidence unavailable')
        return {**observation,'verified':True,'artifact_id':artifact['id'],'artifact_sha256':artifact['sha256']}


def sign_jwt(message,key):
    """Use the host crypto tool; never pass a private key through command arguments."""
    with tempfile.TemporaryDirectory(prefix='distribution-app-') as directory:
        path = os.path.join(directory,'key.pem')
        fd = os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as stream:
            stream.write(key)
        process = subprocess.run(['openssl','dgst','-sha256','-sign',path],input=message,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30,
            env={k:v for k,v in os.environ.items() if k in {'PATH','TMPDIR','HOME'}})
        require(process.returncode == 0 and process.stdout, 'App signing unavailable')
        return process.stdout
