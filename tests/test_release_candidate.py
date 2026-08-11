import copy
import hashlib
from pathlib import Path
import unittest
from unittest import mock

from modules import release_candidate


CANDIDATE = 'a' * 40
EVIDENCE = 'b' * 40


def manifest_value(*, gate_status='pending'):
    source = {
        path: hashlib.sha256(path.encode()).hexdigest()
        for path in release_candidate.REQUIRED_SOURCE_PATHS
    }
    findings = [
        {'id': finding, 'status': 'resolved', 'checkpoints': [EVIDENCE]}
        for finding in release_candidate.REQUIRED_FINDINGS
    ]
    gates = {
        name: {
            'status': gate_status,
            'candidate_sha': CANDIDATE,
            'command': f'validate {name}',
            'total': 1,
            'passed': 1,
            'skipped': 0,
            'failures': [],
            'evidence': [f'{name} evidence'],
        }
        for name in release_candidate.REQUIRED_GATES
    }
    return {
        'schema_version': 1,
        'release_id': 'modernization-rc1',
        'candidate_sha': CANDIDATE,
        'rollback_sha': release_candidate.ROLLBACK_SHA,
        'branch': release_candidate.BRANCH,
        'source_digests': source,
        'production_plan': {
            'expected_bot_id': release_candidate.PRODUCTION_BOT_ID,
            'database': release_candidate.PRODUCTION_DATABASE,
            'api_enabled': False,
            'global_commands_expected_empty': True,
            'guilds': [
                {
                    'guild_id': release_candidate.MAIN_GUILD_ID,
                    'name': 'Polytopia Main',
                    'staff_help_channel': 742857671237042176,
                    'first_helper_role': 'ELO-Helper',
                    'capabilities': ['tools_support'],
                },
                {
                    'guild_id': release_candidate.POLYCHAMPIONS_GUILD_ID,
                    'name': 'PolyChampions',
                    'staff_help_channel': 742832436047511572,
                    'first_helper_role': 'Helper',
                    'capabilities': [
                        'core_user', 'house', 'league', 'squad', 'team',
                        'tools_support',
                    ],
                },
            ],
            'omitted_capabilities': [
                'beta_testing', 'elo_maintenance', 'operator',
            ],
        },
        'adversarial_findings': findings,
        'gates': gates,
    }


class ReleaseCandidateTests(unittest.TestCase):
    def test_complete_pass_record_is_ready(self):
        manifest = release_candidate.validate(manifest_value(gate_status='pass'))

        self.assertEqual(manifest.candidate_sha, CANDIDATE)
        self.assertEqual(manifest.blockers, ())

    def test_pending_gates_are_valid_but_not_ready(self):
        manifest = release_candidate.validate(manifest_value())

        self.assertEqual(
            manifest.blockers,
            tuple(f'{name} is pending' for name in release_candidate.REQUIRED_GATES),
        )
        self.assertFalse(release_candidate.summary(manifest)['ready'])

    def test_missing_gate_is_rejected(self):
        value = manifest_value()
        del value['gates']['offline_suite']

        with self.assertRaisesRegex(
                release_candidate.ReleaseCandidateError, 'omits a required'):
            release_candidate.validate(value)

    def test_gate_from_another_candidate_is_rejected(self):
        value = manifest_value()
        value['gates']['offline_suite']['candidate_sha'] = 'c' * 40

        with self.assertRaisesRegex(
                release_candidate.ReleaseCandidateError, 'another candidate'):
            release_candidate.validate(value)

    def test_pass_gate_cannot_hide_failure(self):
        value = manifest_value(gate_status='pass')
        gate = value['gates']['offline_suite']
        gate['passed'] = 0
        gate['failures'] = ['missing dependency']

        with self.assertRaisesRegex(
                release_candidate.ReleaseCandidateError, 'cannot pass with failures'):
            release_candidate.validate(value)

    def test_missing_review_finding_is_rejected(self):
        value = manifest_value()
        value['adversarial_findings'].pop()

        with self.assertRaisesRegex(
                release_candidate.ReleaseCandidateError, 'omits a reviewed finding'):
            release_candidate.validate(value)

    def test_development_guild_cannot_replace_polychampions(self):
        value = manifest_value()
        value['production_plan']['guilds'][1]['guild_id'] = 478571892832206869

        with self.assertRaisesRegex(
                release_candidate.ReleaseCandidateError, 'support/canary plan'):
            release_candidate.validate(value)

    def test_wrong_or_partial_critical_digest_set_is_rejected(self):
        value = manifest_value()
        value['source_digests'].pop('uv.lock')

        with self.assertRaisesRegex(
                release_candidate.ReleaseCandidateError, 'exact critical files'):
            release_candidate.validate(value)

    def test_repository_verification_hashes_candidate_tree(self):
        manifest = release_candidate.validate(manifest_value())
        content_by_path = {
            path: path.encode() for path in release_candidate.REQUIRED_SOURCE_PATHS
        }

        def git(_root, *args, **_kwargs):
            if args[:2] == ('rev-parse', 'HEAD'):
                return EVIDENCE.encode() + b'\n'
            if args[0] == 'show':
                path = args[1].split(':', 1)[1]
                return content_by_path[path]
            return b''

        with mock.patch.object(release_candidate, '_git', side_effect=git) as called:
            release_candidate.verify_repository(manifest, Path('/tmp/example'))

        self.assertTrue(any(call.args[1] == 'show' for call in called.call_args_list))

    def test_repository_rejects_finding_outside_candidate(self):
        manifest = release_candidate.validate(manifest_value())

        def git(_root, *args, **_kwargs):
            if args[:2] == ('rev-parse', 'HEAD'):
                return CANDIDATE.encode() + b'\n'
            if args[:2] == ('merge-base', '--is-ancestor') and args[2] == EVIDENCE:
                raise release_candidate.ReleaseCandidateError('not ancestor')
            if args[0] == 'show':
                path = args[1].split(':', 1)[1]
                return path.encode()
            return b''

        with mock.patch.object(release_candidate, '_git', side_effect=git):
            with self.assertRaisesRegex(
                    release_candidate.ReleaseCandidateError, 'outside the candidate'):
                release_candidate.verify_repository(manifest, Path('/tmp/example'))

    def test_extra_fields_are_rejected(self):
        value = copy.deepcopy(manifest_value())
        value['password'] = 'not allowed'

        with self.assertRaisesRegex(
                release_candidate.ReleaseCandidateError, 'reviewed fields'):
            release_candidate.validate(value)

    def test_secret_material_is_rejected(self):
        value = manifest_value()
        value['gates']['cutover_review']['evidence'] = [
            'postgresql://operator:secret@example.invalid/polytopia2',
        ]

        with self.assertRaisesRegex(
                release_candidate.ReleaseCandidateError, 'secret material'):
            release_candidate.validate(value)


if __name__ == '__main__':
    unittest.main()
