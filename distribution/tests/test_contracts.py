import copy
import hashlib
import json
from pathlib import Path
import unittest

from distribution import contracts

FIXTURES = Path(__file__).parent / 'fixtures'


def registration(app='patronus'):
    return json.loads((FIXTURES / (app + '-registration.json')).read_text())


def approve(document):
    """Invented owner evidence only, never a production approval."""
    doc = copy.deepcopy(document)
    digest = hashlib.sha256(json.dumps(doc['configuration'], sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    doc['configuration_digest'] = digest
    doc['approval']['configuration_digest'] = digest
    return doc


class RegistrationTests(unittest.TestCase):
    def test_another_app_cannot_claim_patronus_formula(self):
        doc = registration('lumen')
        doc['configuration']['destinations']['brew']['path'] = 'Formula/patronus.rb'
        with self.assertRaises(ValueError):
            contracts.validate_registration(approve(doc))

    def test_only_exact_owner_approved_configuration_is_accepted(self):
        good = registration()
        self.assertEqual(contracts.validate_registration(good), good)
        for mutate in [
            lambda d: d['approval'].update(owner_id=123),
            lambda d: d['configuration'].update(description='unapproved edit'),
            lambda d: d['approval'].update(configuration_digest='0'*64),
            lambda d: d.update(authority='producer'),
        ]:
            doc = copy.deepcopy(good)
            mutate(doc)
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                contracts.validate_registration(doc)

    def test_unregistered_paths_sources_and_unknown_authority_are_denied(self):
        for mutate in [
            lambda c: c.update(repository='attacker/patronus'),
            lambda c: c.update(owner_id=123),
            lambda c: c.update(history_branch='../main'),
            lambda c: c.update(release_workflow_path='../../run.py'),
            lambda c: c.update(dispatcher_actor_id=True),
            lambda c: c.update(extra_authority=True),
            lambda c: c['destinations']['scoop'].update(path='bucket/../patronus.json'),
            lambda c: c['destinations']['scoop'].update(repository='attacker/bucket'),
            lambda c: c['destinations']['winget'].update(path='manifests/d/darkquasar/Patronus'),
            lambda c: c['assets'].update({'linux-amd64':'../patronus'}),
            lambda c: c['assets'].update({'linux-386':'unexpected'}),
            lambda c: c['identifiers'].update(command='patronus; echo unsafe'),
        ]:
            doc = registration('lumen')
            mutate(doc['configuration'])
            with self.subTest(config=doc['configuration']), self.assertRaises(ValueError):
                contracts.validate_registration(approve(doc))

    def test_canonical_payload_has_no_approval_cycle(self):
        doc = registration()
        changed = copy.deepcopy(doc)
        changed['approval']['reference'] = 'fixture:other-reference'
        self.assertEqual(contracts.validate_registration(changed)['configuration_digest'],
                         doc['configuration_digest'])
        self.assertEqual(contracts.canonical_digest({'a':1,'b':'é'}),
                         contracts.canonical_digest({'b':'é','a':1}))
        for value in [float('nan'), 1.5, {1:'key'}, {'nested':[float('inf')]}]:
            with self.assertRaises(ValueError):
                contracts.canonical_digest(value)

    def test_duplicate_json_keys_and_noncanonical_numbers_are_rejected(self):
        from distribution.snapshot import strict_json
        for data in [b'{"owner":1,"owner":2}',b'{"nested":{"x":1,"x":2}}',
                     b'{"size":1.0}',b'{"value":NaN}']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                strict_json(data)

    def test_both_independent_apps_are_valid_and_returned_values_are_isolated(self):
        for app in ['patronus','lumen']:
            doc = registration(app)
            validated = contracts.validate_registration(doc)
            validated['configuration']['description'] = 'changed caller copy'
            self.assertNotEqual(doc['configuration']['description'],validated['configuration']['description'])
