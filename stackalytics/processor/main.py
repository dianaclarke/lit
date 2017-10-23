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

import itertools

import jsonschema
from oslo_config import cfg
from oslo_log import log as logging
import psutil
import six

from stackalytics.processor import config
from stackalytics.processor import default_data_processor
from stackalytics.processor import rcs
from stackalytics.processor import record_processor
from stackalytics.processor import runtime_storage
from stackalytics.processor import schema
from stackalytics.processor import utils
from stackalytics.processor import vcs

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


def get_pids():
    result = set([])
    for pid in psutil.pids():
        try:
            p = psutil.Process(pid)
            name = p.name()
            if name == 'uwsgi':
                LOG.debug('Found uwsgi process, pid: %s', pid)
                result.add(pid)
        except Exception as e:
            LOG.debug('Exception while iterating process list: %s', e)
            pass

    return result


def update_pids(runtime_storage):
    pids = get_pids()
    if not pids:
        return
    runtime_storage.active_pids(pids)


def _merge_commits(original, new):
    if new['branches'] < original['branches']:
        return False
    else:
        original['branches'] |= new['branches']
        return True


def _record_typer(record_iterator, record_type):
    for record in record_iterator:
        record['record_type'] = record_type
        yield record


def _get_repo_branches(repo):
    return ({repo.get('default_branch', 'master')} |
            set(r['branch'] for r in repo.get('releases', [])
                if 'branch' in r))


def _process_repo_reviews(repo, runtime_storage_inst, record_processor_inst,
                          rcs_inst):
    for branch in _get_repo_branches(repo):
        LOG.info('Processing reviews for repo: %s, branch: %s',
                 repo['uri'], branch)

        quoted_uri = six.moves.urllib.parse.quote_plus(repo['uri'])
        rcs_key = 'rcs:%s:%s' % (quoted_uri, branch)
        last_retrieval_time = runtime_storage_inst.get_by_key(rcs_key)
        current_retrieval_time = utils.date_to_timestamp('now')

        review_iterator = itertools.chain(
            rcs_inst.log(repo, branch, last_retrieval_time, status='open'),
            rcs_inst.log(repo, branch, last_retrieval_time, status='merged'),
            rcs_inst.log(repo, branch, last_retrieval_time, status='abandoned',
                         grab_comments=True), )

        review_iterator_typed = _record_typer(review_iterator, 'review')
        processed_review_iterator = record_processor_inst.process(
            review_iterator_typed)

        runtime_storage_inst.set_records(processed_review_iterator,
                                         utils.merge_records)
        runtime_storage_inst.set_by_key(rcs_key, current_retrieval_time)


def _process_repo_vcs(repo, runtime_storage_inst, record_processor_inst):
    vcs_inst = vcs.get_vcs(repo, CONF.sources_root)
    vcs_inst.fetch()

    for branch in _get_repo_branches(repo):
        LOG.info('Processing commits in repo: %s, branch: %s',
                 repo['uri'], branch)

        quoted_uri = six.moves.urllib.parse.quote_plus(repo['uri'])
        vcs_key = 'vcs:%s:%s' % (quoted_uri, branch)
        last_id = runtime_storage_inst.get_by_key(vcs_key)

        commit_iterator = vcs_inst.log(branch, last_id)
        commit_iterator_typed = _record_typer(commit_iterator, 'commit')
        processed_commit_iterator = record_processor_inst.process(
            commit_iterator_typed)
        runtime_storage_inst.set_records(
            processed_commit_iterator, _merge_commits)

        last_id = vcs_inst.get_last_id(branch)
        runtime_storage_inst.set_by_key(vcs_key, last_id)


def _process_repo(repo, runtime_storage_inst, record_processor_inst,
                  rcs_inst):
    LOG.info('Processing repo: %s', repo['uri'])

    _process_repo_vcs(repo, runtime_storage_inst, record_processor_inst)

    if 'has_gerrit' in repo:
        _process_repo_reviews(repo, runtime_storage_inst,
                              record_processor_inst, rcs_inst)


def _post_process_records(record_processor_inst, repos):
    LOG.debug('Build release index')
    release_index = {}
    for repo in repos:
        vcs_inst = vcs.get_vcs(repo, CONF.sources_root)
        release_index.update(vcs_inst.fetch())

    LOG.debug('Post-process all records')
    record_processor_inst.post_processing(release_index)


def process(runtime_storage_inst, record_processor_inst):
    repos = utils.load_repos(runtime_storage_inst)

    rcs_inst = rcs.get_rcs(CONF.review_uri)
#     rcs_inst.setup(key_filename=CONF.ssh_key_filename,
#                    username=CONF.ssh_username,
#                    gerrit_retry=CONF.gerrit_retry)

    for repo in repos:
        _process_repo(repo, runtime_storage_inst, record_processor_inst,
                      rcs_inst)

    rcs_inst.close()

    _post_process_records(record_processor_inst, repos)


def process_project_list(runtime_storage_inst):
    module_groups = runtime_storage_inst.get_by_key('module_groups') or {}

    # register modules as module groups
    repos = runtime_storage_inst.get_by_key('repos') or []
    for repo in repos:
        module = repo['module'].lower()
        module_groups[module] = utils.make_module_group(module, tag='module')

    # register module 'unknown' - used for emails not mapped to any module
    module_groups['unknown'] = utils.make_module_group('unknown', tag='module')

    runtime_storage_inst.set_by_key('module_groups', module_groups)


def main():
    utils.init_config_and_logging(config.CONNECTION_OPTS +
                                  config.PROCESSOR_OPTS)

    runtime_storage_inst = runtime_storage.get_runtime_storage(
        CONF.runtime_storage_uri)

    default_data = utils.read_json_from_uri(CONF.default_data_uri)
    if not default_data:
        LOG.critical('Unable to load default data')
        return not 0

    try:
        jsonschema.validate(default_data, schema.default_data)
    except jsonschema.ValidationError as e:
        LOG.critical('The default data is invalid: %s' % e)
        return not 0

    default_data_processor.process(runtime_storage_inst,
                                   default_data)

    process_project_list(runtime_storage_inst)

    update_pids(runtime_storage_inst)

    record_processor_inst = record_processor.RecordProcessor(
        runtime_storage_inst)

    process(runtime_storage_inst, record_processor_inst)

    runtime_storage_inst.set_by_key('runtime_storage_update_time',
                                    utils.date_to_timestamp('now'))
    LOG.info('stackalytics-processor succeeded.')


if __name__ == '__main__':
    main()
