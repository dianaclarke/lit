# Copyright (c) 2013 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time

import mock
from oslo_config import cfg
import six
import testtools

from stackalytics.processor import config
from stackalytics.processor import record_processor
from stackalytics.processor import runtime_storage
from stackalytics.processor import user_processor
from stackalytics.processor.user_processor import get_company_by_email
from stackalytics.processor import utils


CONF = cfg.CONF

RELEASES = [
    {
        'release_name': 'prehistory',
        'end_date': utils.date_to_timestamp('2011-Apr-21')
    },
    {
        'release_name': 'Diablo',
        'end_date': utils.date_to_timestamp('2011-Sep-08')
    },
    {
        'release_name': 'Zoo',
        'end_date': utils.date_to_timestamp('2035-Sep-08')
    },
]

REPOS = [
    {
        "branches": ["master"],
        "module": "stackalytics",
        "project_type": "stackforge",
        "uri": "git://git.openstack.org/stackforge/stackalytics.git"
    }
]


class TestRecordProcessor(testtools.TestCase):
    def setUp(self):
        super(TestRecordProcessor, self).setUp()
        CONF.register_opts(config.CONNECTION_OPTS + config.PROCESSOR_OPTS)

    def test_get_company_by_email_mapped(self):
        record_processor_inst = self.make_record_processor(
            companies=[{'company_name': 'IBM', 'domains': ['ibm.com']}]
        )
        email = 'jdoe@ibm.com'
        res = get_company_by_email(record_processor_inst.domains_index, email)
        self.assertEqual('IBM', res)

    def test_get_company_by_email_with_long_suffix_mapped(self):
        record_processor_inst = self.make_record_processor(
            companies=[{'company_name': 'NEC', 'domains': ['nec.co.jp']}]
        )
        email = 'man@mxw.nes.nec.co.jp'
        res = get_company_by_email(record_processor_inst.domains_index, email)
        self.assertEqual('NEC', res)

    def test_get_company_by_email_with_long_suffix_mapped_2(self):
        record_processor_inst = self.make_record_processor(
            companies=[{'company_name': 'NEC',
                        'domains': ['nec.co.jp', 'nec.com']}]
        )
        email = 'man@mxw.nes.nec.com'
        res = get_company_by_email(record_processor_inst.domains_index, email)
        self.assertEqual('NEC', res)

    def test_get_company_by_email_not_mapped(self):
        record_processor_inst = self.make_record_processor()
        email = 'foo@boo.com'
        res = get_company_by_email(record_processor_inst.domains_index, email)
        self.assertIsNone(res)

    def test_process_commit_existing_user(self):
        record_processor_inst = self.make_record_processor(
            users=[
                {
                    'user_id': 'john_doe',
                    'user_name': 'John Doe',
                    'emails': ['johndoe@gmail.com', 'johndoe@nec.co.jp'],
                    'companies': [
                        {'company_name': '*independent',
                         'end_date': 1234567890},
                        {'company_name': 'NEC',
                         'end_date': 0},
                    ]
                }
            ])

        processed_commit = list(record_processor_inst.process(
            generate_commits(author_email='johndoe@gmail.com',
                             author_name='John Doe')))[0]

        expected_commit = {
            'user_id': 'john_doe',
            'author_email': 'johndoe@gmail.com',
            'author_name': 'John Doe',
            'company_name': 'NEC',
        }

        self.assertRecordsMatch(expected_commit, processed_commit)

    def test_process_commit_existing_user_old_job(self):
        record_processor_inst = self.make_record_processor(
            users=[
                {
                    'user_id': 'john_doe',
                    'user_name': 'John Doe',
                    'emails': ['johndoe@gmail.com', 'johndoe@nec.co.jp'],
                    'companies': [
                        {'company_name': '*independent',
                         'end_date': 1234567890},
                        {'company_name': 'NEC',
                         'end_date': 0},
                    ]
                }
            ])

        processed_commit = list(record_processor_inst.process(
            generate_commits(author_email='johndoe@gmail.com',
                             author_name='John Doe',
                             date=1000000000)))[0]

        expected_commit = {
            'user_id': 'john_doe',
            'author_email': 'johndoe@gmail.com',
            'author_name': 'John Doe',
            'company_name': '*independent',
        }

        self.assertRecordsMatch(expected_commit, processed_commit)

    def test_process_commit_new_user_unknown(self):
        record_processor_inst = self.make_record_processor(
            companies=[{'company_name': 'IBM', 'domains': ['ibm.com']}])

        processed_commit = list(record_processor_inst.process(
            generate_commits(author_email='johndoe@ibm.com',
                             author_name='John Doe')))[0]

        expected_commit = {
            'author_email': 'johndoe@ibm.com',
            'author_name': 'John Doe',
            'company_name': 'IBM',
        }

        self.assertRecordsMatch(expected_commit, processed_commit)
        user = user_processor.load_user(
            record_processor_inst.runtime_storage_inst,
            user_id='johndoe@ibm.com')
        self.assertIn('johndoe@ibm.com', user['emails'])
        self.assertEqual('IBM', user['companies'][0]['company_name'])

    def test_mark_disagreement(self):
        record_processor_inst = self.make_record_processor(
            users=[
                {'user_id': 'john_doe',
                 'user_name': 'John Doe',
                 'emails': ['john_doe@ibm.com'],
                 'core': [('nova', 'master')],
                 'companies': [{'company_name': 'IBM', 'end_date': 0}]}
            ],
        )
        timestamp = int(time.time())
        runtime_storage_inst = record_processor_inst.runtime_storage_inst
        runtime_storage_inst.set_records(record_processor_inst.process([
            {'record_type': 'review',
             'id': 'I1045730e47e9e6ad31fcdfbaefdad77e2f3b2c3e',
             'subject': 'Fix AttributeError in Keypair._add_details()',
             'owner': {'name': 'John Doe',
                       'email': 'john_doe@ibm.com',
                       'username': 'john_doe'},
             'createdOn': timestamp,
             'module': 'nova',
             'branch': 'master',
             'status': 'NEW',
             'patchSets': [
                 {'number': '1',
                  'revision': '4d8984e92910c37b7d101c1ae8c8283a2e6f4a76',
                  'ref': 'refs/changes/16/58516/1',
                  'uploader': {
                      'name': 'Bill Smith',
                      'email': 'bill@smith.to',
                      'username': 'bsmith'},
                  'createdOn': timestamp,
                  'approvals': [
                      {'type': 'Code-Review', 'description': 'Code Review',
                       'value': '2', 'grantedOn': timestamp - 1,
                       'by': {
                           'name': 'Homer Simpson',
                           'email': 'hsimpson@gmail.com',
                           'username': 'homer'}},
                      {'type': 'Code-Review', 'description': 'Code Review',
                       'value': '-2', 'grantedOn': timestamp,
                       'by': {
                           'name': 'John Doe',
                           'email': 'john_doe@ibm.com',
                           'username': 'john_doe'}}
                  ]
                  },
                 {'number': '2',
                  'revision': '4d8984e92910c37b7d101c1ae8c8283a2e6f4a76',
                  'ref': 'refs/changes/16/58516/1',
                  'uploader': {
                      'name': 'Bill Smith',
                      'email': 'bill@smith.to',
                      'username': 'bsmith'},
                  'createdOn': timestamp + 1,
                  'approvals': [
                      {'type': 'Code-Review', 'description': 'Code Review',
                       'value': '1', 'grantedOn': timestamp + 2,
                       'by': {
                           'name': 'Homer Simpson',
                           'email': 'hsimpson@gmail.com',
                           'username': 'homer'}},
                      {'type': 'Code-Review', 'description': 'Code Review',
                       'value': '-1', 'grantedOn': timestamp + 3,
                       'by': {
                           'name': 'Bart Simpson',
                           'email': 'bsimpson@gmail.com',
                           'username': 'bart'}},
                      {'type': 'Code-Review', 'description': 'Code Review',
                       'value': '2', 'grantedOn': timestamp + 4,
                       'by': {
                           'name': 'John Doe',
                           'email': 'john_doe@ibm.com',
                           'username': 'john_doe'}}
                  ]
                  }
             ]}
        ]))
        record_processor_inst.post_processing({})

        marks = list([r for r in runtime_storage_inst.get_all_records()
                      if r['record_type'] == 'mark'])

        homer_mark = next(six.moves.filter(
            lambda x: x['date'] == (timestamp - 1), marks), None)
        self.assertTrue(homer_mark.get('disagreement'),
                        msg='Disagreement: core set -2 after +2')

        homer_mark = next(six.moves.filter(
            lambda x: x['date'] == (timestamp + 2), marks), None)
        self.assertFalse(homer_mark.get('disagreement'),
                         msg='No disagreement: core set +2 after +1')

        bart_mark = next(six.moves.filter(
            lambda x: x['date'] == (timestamp + 3), marks), None)
        self.assertTrue(bart_mark.get('disagreement'),
                        msg='Disagreement: core set +2 after -1')

    def test_commit_merge_date(self):
        record_processor_inst = self.make_record_processor()
        runtime_storage_inst = record_processor_inst.runtime_storage_inst

        runtime_storage_inst.set_records(record_processor_inst.process([
            {'record_type': 'commit',
             'commit_id': 'de7e8f2',
             'change_id': ['I104573'],
             'author_name': 'John Doe',
             'author_email': 'john_doe@gmail.com',
             'date': 1234567890,
             'lines_added': 25,
             'lines_deleted': 9,
             'module': u'stackalytics',
             'release_name': 'havana'},
            {'record_type': 'review',
             'id': 'I104573',
             'subject': 'Fix AttributeError in Keypair._add_details()',
             'owner': {'name': 'John Doe',
                       'email': 'john_doe@gmail.com',
                       'username': 'john_doe'},
             'createdOn': 1385478465,
             'lastUpdated': 1385490000,
             'status': 'MERGED',
             'module': 'nova', 'branch': 'master'},
        ]))
        record_processor_inst.post_processing({})

        commit = runtime_storage_inst.get_by_primary_key('de7e8f2')
        self.assertEqual(1385490000, commit['date'])

    def test_commit_module_alias(self):
        record_processor_inst = self.make_record_processor()
        runtime_storage_inst = record_processor_inst.runtime_storage_inst

        with mock.patch('stackalytics.processor.utils.load_repos') as patch:
            patch.return_value = [{'module': 'sahara', 'aliases': ['savanna']}]
            runtime_storage_inst.set_records(record_processor_inst.process([
                {'record_type': 'commit',
                 'commit_id': 'de7e8f2',
                 'change_id': ['I104573'],
                 'author_name': 'John Doe',
                 'author_email': 'john_doe@gmail.com',
                 'date': 1234567890,
                 'lines_added': 25,
                 'lines_deleted': 9,
                 'module': u'savanna',
                 'release_name': 'havana'},
                {'record_type': 'review',
                 'id': 'I104573',
                 'subject': 'Fix AttributeError in Keypair._add_details()',
                 'owner': {'name': 'John Doe',
                           'email': 'john_doe@gmail.com',
                           'username': 'john_doe'},
                 'createdOn': 1385478465,
                 'lastUpdated': 1385490000,
                 'status': 'MERGED',
                 'module': 'nova', 'branch': 'master'},
            ]))
            record_processor_inst.post_processing({})

        commit = runtime_storage_inst.get_by_primary_key('de7e8f2')
        self.assertEqual('sahara', commit['module'])

    def test_get_modules(self):
        record_processor_inst = self.make_record_processor()
        with mock.patch('stackalytics.processor.utils.load_repos') as patch:
            patch.return_value = [{'module': 'nova'},
                                  {'module': 'python-novaclient'},
                                  {'module': 'neutron'},
                                  {'module': 'sahara', 'aliases': ['savanna']}]
            modules, module_alias_map = record_processor_inst._get_modules()
            self.assertEqual(set(['nova', 'neutron', 'sahara', 'savanna']),
                             set(modules))
            self.assertEqual({'savanna': 'sahara'}, module_alias_map)

    def test_guess_module(self):
        record_processor_inst = self.make_record_processor()
        with mock.patch('stackalytics.processor.utils.load_repos') as patch:
            patch.return_value = [{'module': 'sahara', 'aliases': ['savanna']}]
            record = {'subject': '[savanna] T'}
            record_processor_inst._guess_module(record)
            self.assertEqual({'subject': '[savanna] T', 'module': 'sahara'},
                             record)

    def assertRecordsMatch(self, expected, actual):
        for key, value in six.iteritems(expected):
            self.assertEqual(value, actual.get(key),
                             'Values for key %s do not match' % key)

    def make_record_processor(self, users=None, companies=None, releases=None,
                              repos=None):
        rp = record_processor.RecordProcessor(make_runtime_storage(
            users=users, companies=companies, releases=releases, repos=repos))

        return rp


def generate_commits(author_name='John Doe', author_email='johndoe@gmail.com',
                     date=1999999999):
    yield {
        'record_type': 'commit',
        'commit_id': 'de7e8f297c193fb310f22815334a54b9c76a0be1',
        'author_name': author_name,
        'author_email': author_email,
        'date': date,
        'lines_added': 25,
        'lines_deleted': 9,
        'release_name': 'havana',
    }


def make_runtime_storage(users=None, companies=None, releases=None,
                         repos=None):
    runtime_storage_cache = {}
    runtime_storage_record_keys = []

    def get_by_key(key):
        if key == 'companies':
            return _make_companies(companies or [
                {"company_name": "*independent", "domains": [""]},
            ])
        elif key == 'users':
            return _make_users(users or [])
        elif key == 'releases':
            return releases or RELEASES
        elif key == 'repos':
            return repos or REPOS
        else:
            return runtime_storage_cache.get(key)

    def set_by_key(key, value):
        runtime_storage_cache[key] = value

    def delete_by_key(key):
        del runtime_storage_cache[key]

    def inc_user_count():
        count = runtime_storage_cache.get('user:count') or 0
        count += 1
        runtime_storage_cache['user:count'] = count
        return count

    def get_all_users():
        for n in six.moves.range(
                0, (runtime_storage_cache.get('user:count') or 0) + 1):
            u = runtime_storage_cache.get('user:%s' % n)
            if u:
                yield u

    def set_records(records_iterator):
        for record in records_iterator:
            runtime_storage_cache[record['primary_key']] = record
            runtime_storage_record_keys.append(record['primary_key'])

    def get_all_records():
        return [runtime_storage_cache[key]
                for key in runtime_storage_record_keys]

    def get_by_primary_key(primary_key):
        return runtime_storage_cache.get(primary_key)

    rs = mock.Mock(runtime_storage.RuntimeStorage)
    rs.get_by_key = mock.Mock(side_effect=get_by_key)
    rs.set_by_key = mock.Mock(side_effect=set_by_key)
    rs.delete_by_key = mock.Mock(side_effect=delete_by_key)
    rs.inc_user_count = mock.Mock(side_effect=inc_user_count)
    rs.get_all_users = mock.Mock(side_effect=get_all_users)
    rs.set_records = mock.Mock(side_effect=set_records)
    rs.get_all_records = mock.Mock(side_effect=get_all_records)
    rs.get_by_primary_key = mock.Mock(side_effect=get_by_primary_key)

    if users:
        for user in users:
            set_by_key('user:%s' % user['user_id'], user)
            for email in user.get('emails') or []:
                set_by_key('user:%s' % email, user)

    return rs


def _make_users(users):
    users_index = {}
    for user in users:
        if 'user_id' in user:
            users_index[user['user_id']] = user
        for email in user['emails']:
            users_index[email] = user
    return users_index


def _make_companies(companies):
    domains_index = {}
    for company in companies:
        for domain in company['domains']:
            domains_index[domain] = company['company_name']
    return domains_index
