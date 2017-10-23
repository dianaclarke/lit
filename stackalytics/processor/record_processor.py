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

import bisect
import collections
import copy
import functools
import time

from oslo_config import cfg
from oslo_log import log as logging
import six

from stackalytics.processor import launchpad_utils
from stackalytics.processor import user_processor
from stackalytics.processor import utils


CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class RecordProcessor(object):
    def __init__(self, runtime_storage_inst):
        self.runtime_storage_inst = runtime_storage_inst

        self.domains_index = runtime_storage_inst.get_by_key('companies')

        self.releases = runtime_storage_inst.get_by_key('releases')
        self.releases_dates = [r['end_date'] for r in self.releases]

        self.modules = None
        self.alias_module_map = None

    def _get_release(self, timestamp):
        release_index = bisect.bisect(self.releases_dates, timestamp)
        if release_index >= len(self.releases):
            LOG.warning('Timestamp %s is beyond releases boundaries, the last '
                        'release will be used. Please consider adding a '
                        'new release into default_data.json', timestamp)
            release_index = len(self.releases) - 1
        return self.releases[release_index]['release_name']

    def _get_modules(self):
        if self.modules is None:
            self.modules = set()
            self.alias_module_map = dict()

            for repo in utils.load_repos(self.runtime_storage_inst):
                module = repo['module'].lower()
                module_aliases = repo.get('aliases') or []

                add = True
                for module_name in ([module] + module_aliases):
                    for m in self.modules:
                        if module_name.find(m) >= 0:
                            add = False
                            break
                        if m.find(module_name) >= 0:
                            self.modules.remove(m)
                            break
                    if add:
                        self.modules.add(module_name)

                for alias in module_aliases:
                    self.alias_module_map[alias] = module

        return self.modules, self.alias_module_map

    def _need_to_fetch_launchpad(self):
        return CONF.fetching_user_source == 'launchpad'

    def _update_user(self, record):
        email = record.get('author_email')
        user_e = user_processor.load_user(
            self.runtime_storage_inst, email=email) or {}

        user_name = record.get('author_name')
        launchpad_id = record.get('launchpad_id')
        if (self._need_to_fetch_launchpad() and email and (not user_e) and
                (not launchpad_id) and (not user_e.get('launchpad_id'))):
            # query LP
            launchpad_id, lp_user_name = launchpad_utils.query_lp_info(email)
            if lp_user_name:
                user_name = lp_user_name

        gerrit_id = record.get('gerrit_id')
        if gerrit_id:
            user_g = user_processor.load_user(
                self.runtime_storage_inst, gerrit_id=gerrit_id) or {}
            if (self._need_to_fetch_launchpad() and (not user_g) and
                    (not launchpad_id) and (not user_e.get('launchpad_id'))):
                # query LP
                guessed_lp_id = gerrit_id
                lp_user_name = launchpad_utils.query_lp_user_name(
                    guessed_lp_id)
                if lp_user_name == user_name:
                    launchpad_id = guessed_lp_id
        else:
            user_g = {}

        user_l = user_processor.load_user(
            self.runtime_storage_inst, launchpad_id=launchpad_id) or {}

        if user_processor.are_users_same([user_e, user_l, user_g]):
            # If sequence numbers are set and the same, merge is not needed
            return user_e

        user = user_processor.create_user(
            self.domains_index, launchpad_id, email, gerrit_id, user_name)

        if user_e or user_l or user_g:
            # merge between existing profiles and a new one
            user, users_to_delete = user_processor.merge_user_profiles(
                self.domains_index, [user_e, user_l, user_g, user])

            # delete all unneeded profiles
            user_processor.delete_users(
                self.runtime_storage_inst, users_to_delete)
        else:
            # create new profile
            if (self._need_to_fetch_launchpad() and not user_name):
                user_name = launchpad_utils.query_lp_user_name(launchpad_id)
                if user_name:
                    user['user_name'] = user_name
            LOG.debug('Created new user: %s', user)

        user_processor.store_user(self.runtime_storage_inst, user)
        LOG.debug('Stored user: %s', user)

        return user

    def _update_record_and_user(self, record):
        user = self._update_user(record)

        record['user_id'] = user['user_id']
        if user.get('user_name'):
            record['author_name'] = user['user_name']

        company, policy = user_processor.get_company_for_date(
            user['companies'], record['date'])

        if not user.get('static'):
            # for auto-generated profiles affiliation may be overridden
            if company != '*robots' and policy == 'open':
                company = (user_processor.get_company_by_email(
                    self.domains_index, record.get('author_email')) or company)

        record['company_name'] = company

    def _process_commit(self, record):
        record['primary_key'] = record['commit_id']
        record['loc'] = record['lines_added'] + record['lines_deleted']
        record['author_email'] = record['author_email'].lower()
        record['commit_date'] = record['date']

        coauthors = record.get('coauthor')
        if not coauthors:
            self._update_record_and_user(record)

            if record['company_name'] != '*robots':
                yield record
        else:
            if record['author_email'] not in [
                    c['author_email'] for c in coauthors]:
                coauthors.append({'author_name': record['author_name'],
                                  'author_email': record['author_email']})
            for coauthor in coauthors:
                coauthor['date'] = record['date']
                self._update_record_and_user(coauthor)

            for coauthor in coauthors:
                new_record = copy.deepcopy(record)
                new_record.update(coauthor)
                new_record['primary_key'] += coauthor['author_email']

                yield new_record

    def _make_review_record(self, record):
        # copy everything except patchsets and flatten user data
        review = dict([(k, v) for k, v in six.iteritems(record)
                       if k not in ['patchSets', 'owner', 'createdOn',
                                    'comments']])
        owner = record['owner']

        review['primary_key'] = review['id']
        if owner.get('username'):
            review['gerrit_id'] = owner['username']
        review['author_name'] = (owner.get('name') or owner.get('username')
                                 or 'Anonymous Coward')  # do it like gerrit
        if owner.get('email'):
            review['author_email'] = owner['email'].lower()
        review['date'] = record['createdOn']

        patch_sets = record.get('patchSets', [])
        review['updated_on'] = review['date']
        if patch_sets:
            patch = patch_sets[-1]
            if 'approvals' in patch:
                review['value'] = min([int(p['value'])
                                       for p in patch['approvals']])
                review['updated_on'] = patch['approvals'][0]['grantedOn']
            else:
                review['updated_on'] = patch['createdOn']

        if 'value' not in review:
            review['value'] = 0

        self._update_record_and_user(review)
        return review

    def _make_patch_record(self, review, patch):
        patch_record = dict()
        patch_record['record_type'] = 'patch'
        patch_record['primary_key'] = utils.get_patch_id(
            review['id'], patch['number'])
        patch_record['number'] = patch['number']
        patch_record['date'] = patch['createdOn']
        uploader = patch['uploader']
        if uploader.get('username'):
            patch_record['gerrit_id'] = uploader['username']
        patch_record['author_name'] = (uploader.get('name')
                                       or uploader.get('username')
                                       or 'Anonymous Coward')
        if uploader.get('email'):
            patch_record['author_email'] = uploader['email'].lower()
        patch_record['module'] = review['module']
        patch_record['branch'] = review['branch']
        patch_record['review_id'] = review['id']

        self._update_record_and_user(patch_record)
        return patch_record

    def _make_mark_record(self, review, patch, approval):
        # copy everything and flatten user data
        mark = dict([(k, v) for k, v in six.iteritems(approval)
                     if k not in ['by', 'grantedOn', 'value', 'description']])
        reviewer = approval['by']

        mark['record_type'] = 'mark'
        mark['value'] = int(approval['value'])
        mark['date'] = approval['grantedOn']
        mark['primary_key'] = (review['id'] + str(mark['date']) + mark['type'])
        mark['gerrit_id'] = reviewer['username']
        mark['author_name'] = reviewer.get('name') or reviewer.get('username')
        mark['author_email'] = reviewer['email'].lower()
        mark['module'] = review['module']
        mark['branch'] = review['branch']
        mark['review_id'] = review['id']
        mark['patch'] = int(patch['number'])

        if reviewer['username'] == patch['uploader'].get('username'):
            # reviewer is the same as author of the patch
            mark['type'] = 'Self-%s' % mark['type']

        self._update_record_and_user(mark)
        return mark

    def _process_review(self, record):
        """Process a review.

        Review spawns into records of three types:
          * review - records that a user created review request
          * patch - records that a user submitted another patch set
          * mark - records that a user set approval mark to given review
        """
        owner = record['owner']
        if 'email' in owner or 'username' in owner:
            yield self._make_review_record(record)

        for patch in record.get('patchSets', []):
            if (('email' in patch['uploader']) or
                    ('username' in patch['uploader'])):
                yield self._make_patch_record(record, patch)

            if 'approvals' not in patch:
                continue  # not reviewed by anyone

            for approval in patch['approvals']:
                if approval['type'] not in ('Code-Review', 'Workflow'):
                    continue  # keep only Code-Review and Workflow
                if ('email' not in approval['by'] or
                        'username' not in approval['by']):
                    continue  # ignore

                yield self._make_mark_record(record, patch, approval)

        # check for abandon action
        if record.get('status') == 'ABANDONED':
            for comment in reversed(record.get('comments') or []):
                if comment['message'] == 'Abandoned':
                    action = dict(type='Abandon', value=0)
                    action['by'] = comment['reviewer']
                    action['grantedOn'] = comment['timestamp']

                    if ('email' not in action['by'] or
                            'username' not in action['by']):
                        continue  # ignore

                    yield self._make_mark_record(
                        record, record['patchSets'][-1], action)

    def _guess_module(self, record):
        subject = record['subject'].lower()
        pos = len(subject)
        best_guess_module = None

        modules, alias_module_map = self._get_modules()
        for module in modules:
            find = subject.find(module)
            if (find >= 0) and (find < pos):
                pos = find
                best_guess_module = module

        if best_guess_module:
            if (((pos > 0) and (subject[pos - 1] == '[')) or
                    (not record.get('module'))):
                record['module'] = best_guess_module

        if not record.get('module'):
            record['module'] = 'unknown'
        elif record['module'] in alias_module_map:
            record['module'] = alias_module_map[record['module']]

    def _process_bug(self, record):

        bug_created = record.copy()
        bug_created['primary_key'] = 'bugf:' + record['id']
        bug_created['record_type'] = 'bugf'
        bug_created['launchpad_id'] = record.get('owner')
        bug_created['date'] = record['date_created']

        self._update_record_and_user(bug_created)

        yield bug_created

        FIXED_BUGS = ['Fix Committed', 'Fix Released']
        if (('date_fix_committed' in record or 'date_fix_released' in record)
                and record['status'] in FIXED_BUGS):
            bug_fixed = record.copy()
            bug_fixed['primary_key'] = 'bugr:' + record['id']
            bug_fixed['record_type'] = 'bugr'
            bug_fixed['launchpad_id'] = record.get('assignee') or '*unassigned'
            # It appears that launchpad automatically sets the
            # date_fix_committed field when a bug moves from an open
            # state to Fix Released, however it isn't clear that this
            # is documented. So, we take the commit date if it is
            # present or the release date if no commit date is
            # present.
            bug_fixed['date'] = (
                record.get('date_fix_committed') or
                record['date_fix_released']
            )

            self._update_record_and_user(bug_fixed)

            yield bug_fixed

    def _renew_record_date(self, record):
        record['week'] = utils.timestamp_to_week(record['date'])
        if ('release' not in record) or (not record['release']):
            record['release'] = self._get_release(record['date'])

    def process(self, record_iterator):
        PROCESSORS = {
            'commit': self._process_commit,
            'review': self._process_review,
            'bug': self._process_bug,
        }

        for record in record_iterator:
            for r in PROCESSORS[record['record_type']](record):
                self._renew_record_date(r)
                yield r

    def _update_records_with_releases(self, release_index):
        LOG.info('Update records with releases')

        def record_handler(record):
            if (record['record_type'] == 'commit'
                    and record['primary_key'] in release_index):
                release = release_index[record['primary_key']]
            else:
                release = self._get_release(record['date'])

            if record['release'] != release:
                record['release'] = release
                yield record

        yield record_handler

    def _update_records_with_user_info(self):
        LOG.info('Update user info in records')

        def record_handler(record):
            company_name = record['company_name']
            user_id = record['user_id']
            author_name = record['author_name']

            self._update_record_and_user(record)

            if ((record['company_name'] != company_name) or
                    (record['user_id'] != user_id) or
                    (record['author_name'] != author_name)):
                LOG.debug('User info (%(id)s, %(name)s, %(company)s) has '
                          'changed in record %(record)s',
                          {'id': user_id, 'name': author_name,
                           'company': company_name, 'record': record})
                yield record

        yield record_handler

    def _update_commits_with_merge_date(self):
        LOG.info('Update commits with merge date')

        change_id_to_date = {}

        def record_handler_pass_1(record):
            if (record['record_type'] == 'review' and
                    record.get('status') == 'MERGED'):
                change_id_to_date[record['id']] = record['lastUpdated']

        yield record_handler_pass_1

        LOG.info('Update commits with merge date: pass 2')

        def record_handler_pass_2(record):
            if record['record_type'] == 'commit':
                change_id_list = record.get('change_id')
                if change_id_list and len(change_id_list) == 1:
                    change_id = change_id_list[0]
                    if change_id in change_id_to_date:
                        old_date = record['date']
                        if old_date != change_id_to_date[change_id]:
                            record['date'] = change_id_to_date[change_id]
                            self._renew_record_date(record)
                            LOG.debug('Date %(date)s has changed in record '
                                      '%(record)s', {'date': old_date,
                                                     'record': record})
                            yield record

        yield record_handler_pass_2

    def _determine_core_contributors(self):
        LOG.info('Determine core contributors')

        module_branches = collections.defaultdict(set)
        quarter_ago = int(time.time()) - 60 * 60 * 24 * 30 * 3  # a quarter ago

        def record_handler(record):
            if (record['record_type'] == 'mark' and
                    record['date'] > quarter_ago and
                    record['value'] in [2, -2]):
                module_branch = (record['module'], record['branch'])
                user_id = record['user_id']
                module_branches[user_id].add(module_branch)

        yield record_handler

        for user in self.runtime_storage_inst.get_all_users():
            core_old = user.get('core')
            user_module_branch = module_branches.get(user['user_id'])
            if user_module_branch:
                user['core'] = list(user_module_branch)
            elif user.get('core'):
                del user['core']

            if user.get('core') != core_old:
                user_processor.store_user(self.runtime_storage_inst, user)

    def _close_patch(self, cores, marks):
        if len(marks) < 2:
            return

        core_mark = 0
        for mark in sorted(marks, key=lambda x: x['date'], reverse=True):

            if core_mark == 0:
                if (mark['module'], mark['branch'], mark['user_id']) in cores:
                    # mark is from core engineer
                    core_mark = mark['value']
                    continue

            disagreement = ((core_mark != 0) and
                            ((core_mark < 0 < mark['value']) or
                             (core_mark > 0 > mark['value'])))
            old_disagreement = mark.get('disagreement', False)
            mark['disagreement'] = disagreement
            if old_disagreement != disagreement:
                yield mark

    def _update_marks_with_disagreement(self):
        LOG.info('Process marks to find disagreements')

        cores = set()
        for user in self.runtime_storage_inst.get_all_users():
            for (module, branch) in (user.get('core') or []):
                cores.add((module, branch, user['user_id']))

        # map from review_id to current patch and list of marks
        marks_per_patch = collections.defaultdict(
            lambda: {'patch_number': 0, 'marks': []})

        def record_handler(record):
            if (record['record_type'] == 'mark' and
                    record['type'] == 'Code-Review'):
                review_id = record['review_id']
                patch_number = record['patch']

                if review_id in marks_per_patch:
                    # review is already seen, check if patch is newer
                    if (marks_per_patch[review_id]['patch_number'] <
                            patch_number):
                        # the patch is new, close the current
                        for processed in self._close_patch(
                                cores, marks_per_patch[review_id]['marks']):
                            yield processed
                        del marks_per_patch[review_id]

                marks_per_patch[review_id]['patch_number'] = patch_number
                marks_per_patch[review_id]['marks'].append(record)

        yield record_handler

        # purge the rest
        for marks_patch in marks_per_patch.values():
            self.runtime_storage_inst.set_records(
                self._close_patch(cores, marks_patch['marks']))

    def _update_commits_with_module_alias(self):
        LOG.info('Update record with aliases')

        modules, alias_module_map = self._get_modules()

        def record_handler(record):
            if record['record_type'] != 'commit':
                return

            rec_module = record.get('module', None)
            if rec_module and rec_module in alias_module_map:
                record['module'] = alias_module_map[rec_module]
                yield record

        yield record_handler

    def post_processing(self, release_index):
        processors = [
            self._update_records_with_user_info,
            self._update_commits_with_merge_date,
            functools.partial(self._update_records_with_releases,
                              release_index),
            self._update_commits_with_module_alias,
            self._determine_core_contributors,
            self._update_marks_with_disagreement,
        ]

        pipeline_processor = utils.make_pipeline_processor(processors)

        self.runtime_storage_inst.set_records(pipeline_processor(
            self.runtime_storage_inst.get_all_records))
