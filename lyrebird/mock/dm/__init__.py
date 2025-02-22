import re
import uuid
import json
import time
import codecs
import shutil
import datetime
import traceback
from pathlib import Path
from jinja2 import Template
from urllib.parse import urlparse
from collections import OrderedDict
from lyrebird import utils, application
from lyrebird.log import get_logger
from lyrebird.application import config
from lyrebird.mock.dm.jsonpath import jsonpath


PROP_FILE_NAME = '.lyrebird_prop'
logger = get_logger()


class DataManager:

    def __init__(self):
        self.root_path: Path = None
        self.display_data_map = {}
        self.id_map = {}
        self.activated_data = OrderedDict()
        self.activated_group = {}
        self.LEVEL_SUPER_ACTIVATED = 2
        self.clipboard = None
        self.save_to_group_id = None
        self.tmp_group = {'id': 'tmp_group', 'type': 'group', 'name': 'tmp-group', 'label': [], 'children': []}
        self.snapshot_import_cache = {}
        self.SNAPSHOT_SUFFIX = '.lb'
        self.COPY_NODE_NAME_SUFFIX = ' - copy'
        self.root = self.get_default_root()
        self._snapshot_workspace = None

    @property
    def snapshot_workspace(self):
        if not self._snapshot_workspace:
            self._snapshot_workspace = Path(application._cm.ROOT) / 'snapshot'
        if not self._snapshot_workspace.exists():
            self._snapshot_workspace.mkdir()
        return self._snapshot_workspace

    @snapshot_workspace.setter
    def snapshot_workspace(self, workspace):
        self._snapshot_workspace = workspace

    def get_default_root(self):
        return {
            'id': str(uuid.uuid4()),
            'name': '$',
            'type': 'group',
            'parent_id': None,
            'label': [],
            'children': []
        }

    def set_adapter(self, adapter_cls):
        # TODO: Load overridden function only
        self._adapter = adapter_cls(self)

    def set_root(self, uri):
        self._adapter._set_root(uri)

    def reload(self):
        self._adapter._reload()

    def get(self, _id):
        """
        Get mock group or data by id
        """
        if not self.root:
            raise RootNotSet
        node = self.id_map.get(_id)
        if not node:
            raise IDNotFound(_id)
        if node.get('type') == 'group' or node.get('type') == None:
            return node
        elif node.get('type') == 'data':
            return self._adapter._load_data(_id)

    # -----
    # Mock operations
    # -----

    def make_data_map_by_group(self, group_ids):
        self.display_data_map = {k:v for k,v in self.root.items() if k!='children'}
        self.display_data_map['children'] = []

        for group_id in group_ids:
            map_pointer = self.display_data_map['children']

            group = self.id_map.get(group_id)
            group_abs_parent_obj = self._get_abs_parent_obj(group)

            # init parent
            # the last node in group['abs_parent_path'] is node itself, expand node itself
            # expand root which is already in the data_map
            for group_info in group_abs_parent_obj[1:-1]:
                parent_id = group_info['id']

                for index, child in enumerate(map_pointer):
                    if parent_id == child['id']:
                        map_pointer = map_pointer[index]['children']
                        break

                else:
                    node = self.id_map.get(parent_id)

                    new_node = {k:v for k,v in node.items() if k != 'children'}
                    new_node['children'] = []
                    map_pointer.append(new_node)
                    # Because of new_node is `append` into map_pointer, new_node is the last child of map_pointer
                    # move map_pointer first, sort after
                    map_pointer = map_pointer[-1]['children']
                    map_pointer.sort(key=lambda x:x.get('name', ''))

            # update node and node's children
            node = self.id_map.get(group_id)
            map_pointer.append(node)

        return self.display_data_map

    def activate(self, search_id):
        """
        Activite data by id
        """
        if not self.root:
            raise RootNotSet
        _node = self._adapter._get_activate_group(search_id)
        if not _node:
            raise IDNotFound(f'ID:{search_id}')

        _id = _node['id']
        node_id_list = self._collect_activate_node(_node, level_lefted=self.LEVEL_SUPER_ACTIVATED)

        ordered_data_id = []
        for node_id in node_id_list:
            activated_node = self.id_map.get(node_id)
            ordered_data_id += self._collect_data_in_node(activated_node)
        data_list = self._adapter._load_data_by_query({'id': ordered_data_id})

        data_map = {d['id']: d for d in data_list}
        self.activated_data.update({i: data_map[i] for i in ordered_data_id if data_map.get(i)})
        self.activated_group[_id] = _node

    def _collect_activate_node(self, node, level_lefted=1):
        id_list = [node['id']]
        if level_lefted <= 0:
            return id_list
        if not node.get('super_id'):
            return id_list
        if node.get('super_id') == node['id']:
            raise SuperIdCannotBeNodeItself(node['id'])

        _super_id = node.get('super_id')
        _super_node = self.id_map.get(_super_id)
        if not _super_node:
            raise IDNotFound(f'Super node ID: {_super_node}')

        id_list.extend(self._collect_activate_node(_super_node, level_lefted=level_lefted-1))
        return id_list

    def _collect_data_in_node(self, node):
        id_list = []
        if node.get('type', '') == 'data':
            return [node['id']]
        elif node.get('type', '') == 'group':
            if 'children' in node:
                for child in node['children']:
                    id_list.extend(self._collect_data_in_node(child))
        return id_list

    def deactivate(self):
        """
        Clear activated data
        """
        self.activated_data = OrderedDict()
        self.activated_group = {}

    def reactive(self):
        origin_activated_group = self.activated_group
        self.deactivate()
        for _group_id in origin_activated_group:
            self.activate(_group_id)

    def get_matched_data(self, flow):
        """
        Find matched mock data from activated data
        """
        _matched_data = []
        for _data_id in self.activated_data:
            _data = self.activated_data[_data_id]
            if self._is_match_rule(flow, _data.get('rule')):
                _matched_data.append(_data)
                break

        for response_data in _matched_data:
            if 'response' not in response_data:
                continue
            if 'data' not in response_data['response']:
                continue
            if not response_data['response']['data']:
                continue
            self._format_respose_data(response_data)

        return _matched_data

    def _format_respose_data(self, flow):
        # TODO render mock data before response, support more functions
        params = {
            'config': config,
            'ip': config.get('ip'),
            'port': config.get('mock.port'),
            'today': datetime.date.today(),
            'now':  datetime.datetime.now()
        }

        try:
            flow_response_data = Template(flow['response']['data'])
            flow['response']['data'] = flow_response_data.render(params)
        except Exception:
            url = flow['request']['url']
            logger.warning(f'Format response data error! {url}') 

    def _is_match_rule(self, flow, rules):
        if not rules:
            return False
        for rule_key, pattern in rules.items():
            targets = self._get_rule_targets(rule_key, flow)
            if targets == []:
                return False
            if not self._is_target_pattern_matched(pattern, targets):
                return False
        return True

    def _get_rule_targets(self, rule_key, flow):
        search_res = jsonpath.search(flow, rule_key)
        if not search_res:
            return []
        return [s.node for s in search_res]

    def _is_target_pattern_matched(self, pattern, targets):
        for target in targets:
            if not utils.TargetMatch.is_match(target, pattern):
                return False
        return True

    # -----
    # Data tree operations
    # -----

    def _get_request_path(self, request):
        path = request.get('path')
        if not path:
            if not request.get('url'):
                return ''
            parsed_url = urlparse(request['url'])
            path = parsed_url.path
        return path

    def _make_data(self, raw_data, **kwargs):
        data = dict(raw_data)
        _data_id = kwargs.get('data_id') or str(uuid.uuid4())
        data['id'] = _data_id
        if 'request' in data:
            # TODO remove it with inspector frontend
            data['request'] = dict(raw_data['request'])

            _data_name = data['name'] if data.get('name') else self._adapter._get_data_name(data)
            _data_rule = data['rule'] if data.get('rule') else self._adapter._get_data_rule(data['request'])
            if 'data' in data['request']:
                data['request']['data'] = self._flow_data_2_str(data['request']['data'])
        else:
            _data_name = data.get('name')
            _data_rule = {'request.url': '(?=.*YOUR-REQUEST-PATH)(?=.*PARAMS)'}
            data['request'] = {}

        if 'response' in data:
            # TODO remove it with inspector frontend
            data['response'] = dict(raw_data['response'])

            if 'data' in data['response']:
                data['response']['data'] = self._flow_data_2_str(data['response']['data'])
        else:
            data['response'] = {}

        # proxy_response will not be saved
        if 'proxy_response' in data:
            del data['proxy_response']

        data['name'] = _data_name
        data['rule'] = _data_rule

        return data

    def add_data(self, parent_id, raw_data, **kwargs):
        if not isinstance(raw_data, dict):
            raise DataObjectSouldBeADict

        parent_node = None
        if parent_id == 'tmp_group':
            parent_node = self.tmp_group
        elif parent_id:
            parent_node = self.id_map.get(parent_id)
            if not parent_node:
                raise IDNotFound(parent_id)
            if parent_node['type'] == 'data':
                raise DataObjectCannotContainAnyOtherObject

        data = self._make_data(raw_data)
        data['parent_id'] = parent_id
        _data_id = data['id']
        _data_name = data.get('name')

        output_path = (kwargs['output'] / _data_id) if kwargs.get('output') else None

        self._adapter._add_data(data, path=output_path)

        if parent_node:
            data_node = {}
            data_node['id'] = _data_id
            data_node['name'] = _data_name
            data_node['type'] = 'data'
            data_node['parent_id'] = parent_id

            # New data added in the head of child list
            parent_node['children'].insert(0, data_node)
            logger.debug(f'*** Add to node {data_node}')
            # Update ID mapping
            self.id_map[_data_id] = data_node
            self._adapter._add_group(data_node)

        return _data_id

    def _flow_data_2_str(self, data):
        if isinstance(data, str):
            return data
        return json.dumps(data, ensure_ascii=False)

    def add_group(self, parent_id, name):
        if parent_id == None:
            parent_node = self.root
        else:
            parent_node = self.id_map.get(parent_id)
        if not parent_node:
            raise IDNotFound(parent_id)
        if parent_node.get('type') == 'data':
            raise DataObjectCannotContainAnyOtherObject
        # Add group
        group_id = str(uuid.uuid4())
        new_group = {
            'id': group_id,
            'name': name,
            'type': 'group',
            'parent_id': parent_id,
            'label': [],
            'children': [],
            'super_id': None
        }
        # New group added in the head of child list
        if 'children' not in parent_node:
            parent_node['children'] = []
        parent_node['children'].insert(0, new_group)
        # Register ID
        self.id_map[group_id] = new_group
        # Save prop
        self._adapter._add_group(new_group)
        return group_id

    def add_group_by_path(self, path):
        parent_name = path.strip('/')
        current = self.root
        if not parent_name:
            return current['id']
        parent_name_list = parent_name.split('/')
        for name in parent_name_list:
            for child in current['children']:
                if name == child['name']:
                    current = child
                    break
            else:
                group_id = self.add_group(current['id'], name)
                current = self.id_map.get(group_id)
        return current['id']

    def delete(self, _id):
        target_node = self.id_map.get(_id)
        if not target_node:
            raise IDNotFound(_id)
        parent_id = target_node.get('parent_id')
        # Remove refs
        if parent_id:
            parent = self.id_map.get(parent_id)
            parent['children'].remove(target_node)
        else:
            self.root['children'].remove(target_node)
        self._delete(_id)

    def _delete(self, _id):
        target_node = self.id_map.get(_id)
        if not target_node:
            raise IDNotFound(_id)
        if 'children' in target_node and len(target_node['children']) > 0:
            for child in target_node['children'][::-1]:
                self._delete(child['id'])
                target_node['children'].pop(-1)
        # Remove from activated_group
        if _id in self.activated_group:
            self.activated_group.pop(_id)
        # Delete from ID mapping
        self.id_map.pop(_id)
        # Delete from mock tree
        self._adapter._delete_group(_id)
        # Delete from file system
        if target_node['type'] == 'data':
            self._adapter._delete_data(_id)

    def cut(self, _id):
        _node = self.id_map.get(_id)
        if not _node:
            raise IDNotFound(_id)
        self.clipboard = {
            'type': 'cut',
            'id': _id,
            'node': _node
        }

    def copy(self, _id):
        _node = self.id_map.get(_id)
        if not _node:
            raise IDNotFound(_id)
        self.clipboard = {
            'type': 'copy',
            'id': _id,
            'node': _node
        }

    def import_(self, node, json=None, path=None):
        self.clipboard = {
            'type': 'import',
            'id': node['id'],
            'node': node,
            'json': json,
            'path': path
        }

    def paste(self, parent_id, **kwargs):
        if not self.clipboard:
            raise NoneClipbord
        _parent_node = self.id_map.get(parent_id)
        _node = self.clipboard['node']
        if not _parent_node:
            raise IDNotFound(parent_id)
        if not _parent_node.get('children'):
            _parent_node['children'] = []
        if self.clipboard['type'] == 'cut':
            _origin_parent = self.id_map.get(_node['parent_id'])
            _origin_parent['children'].remove(_node)
            _parent_node['children'].insert(0, _node)
            _node['parent_id'] = parent_id
            _group_id = _node['id']
            self._adapter._update_group(_node)
        elif self.clipboard['type'] == 'copy':
            new_name = self._get_copy_node_new_name(_node)
            _group_id = self._copy_node(_parent_node, _node, name=new_name, origin_name=_node['name'], **kwargs)
        elif self.clipboard['type'] == 'import':
            _group_id = self._copy_node(_parent_node, _node, **kwargs)
        return _group_id

    def duplicate(self, _id, **kwargs):
        return self._adapter.duplicate(_id)

    def _copy_node(self, parent_node, node, **kwargs):
        new_node = {}
        new_node.update(node)
        new_node['id'] = str(uuid.uuid4())
        new_node['parent_id'] = parent_node['id']
        new_node['name'] = kwargs['name'] if kwargs.get('name') else new_node['name']
        # Add to target node
        if not parent_node.get('children'):
            parent_node['children'] = []
        parent_node['children'].insert(0, new_node)
        # Add node
        self._adapter._add_group(new_node, **kwargs)
        # Register ID
        self.id_map[new_node['id']] = new_node
        if new_node['type'] == 'group':
            kwargs.pop('name') if kwargs.get('name') else None
            kwargs.pop('origin_name') if kwargs.get('origin_name') else None
            new_node['children'] = []
            for child in node['children']:
                self._copy_node(new_node, child, **kwargs)
        elif new_node['type'] == 'data':
            self._copy_data(new_node, node, **kwargs)
        return new_node['id']

    def _copy_data(self, new_data_node, data_node, **kwargs):
        _id = data_node['id']

        if self.clipboard.get('json'):
            event = self.clipboard.get('json', {}).get(_id)
            if not event:
                raise DataNotFound
            prop = {k:v for k,v in event.items()}
        elif self.clipboard.get('path'):
            with codecs.open(Path(self.clipboard['path'])/_id) as f:
                prop = json.load(f)
        else:
            prop = self._adapter._load_data(_id)

        prop['id'] = new_data_node['id']
        prop['name'] = kwargs.pop('name') if kwargs.get('name') else prop['name']
        self._adapter._add_data(prop)

    def _get_copy_node_new_name(self, _node):
        return _node['name'] + self.COPY_NODE_NAME_SUFFIX

    def _sort_children_by_name(self):
        for node_id in self.id_map:
            node = self.id_map[node_id]
            if 'children' not in node:
                # fix mock data group has no children
                if node['type'] == 'group':
                    node['children'] = []
                continue
            node['children'] = sorted(node['children'], key=lambda sub_node: sub_node['name'])

    # -----
    # Conflict checker
    # -----

    def check_conflict(self, _id):
        node = self.id_map.get(_id)
        if not node:
            raise IDNotFound(_id)
        data_array = []

        def _read_data(node):
            if node.get('type') == 'data':
                _data = self._adapter._load_data(node['id'])
                _data['parent_id'] = node['parent_id']
                data_array.append(_data)
            elif node.get('type') == 'group':
                for child in node['children']:
                    _read_data(child)
        _read_data(node)
        return self.check_conflict_data(data_array)

    def activated_data_check_conflict(self):
        data_array = list(self.activated_data.values())
        return self.check_conflict_data(data_array)

    def check_conflict_data(self, data_array):
        conflict_rules = []
        for _data in data_array:
            _rule = _data['rule']
            _hit_data = []
            for _test_data in data_array:
                if self._is_match_rule(_test_data, _rule):
                    _target_node = {
                        'id': _test_data['id'],
                        'name': _test_data['name'],
                        'url': _test_data['request']['url'],
                        'abs_parent_path': self._get_abs_parent_path(_test_data)
                    }
                    _hit_data.append(_target_node)
            if len(_hit_data) > 1:
                _src_node = {
                    'id': _data['id'],
                    'name': _data['name'],
                    'rule': _data['rule'],
                    'abs_parent_path': self._get_abs_parent_path(_data)
                }
                conflict_rules.append(
                    {
                        'data': _src_node,
                        'conflict_data': _hit_data
                    }
                )
        return conflict_rules

    def _get_abs_parent_path(self, node, path='/'):
        parent_node = self._get_node_parent(node)
        if parent_node is None:
            return path
        current_path = '/' + node['name'] + path
        return self._get_abs_parent_path(parent_node, path=current_path)

    def _get_abs_parent_obj(self, node, parent_obj=None):
        if parent_obj is None:
            parent_obj = []
        if 'id' not in node:
            return parent_obj
        node_info = self.id_map.get(node['id'])
        if not node_info:
            return parent_obj
        parent_obj.insert(0, {
            'id': node_info['id'],
            'name': node_info['name'],
            'type': node_info['type'],
            'parent_id': node_info['parent_id']
        })
        parent_node = self._get_node_parent(node)
        if parent_node is None:
            return parent_obj
        return self._get_abs_parent_obj(parent_node, parent_obj=parent_obj)

    def _get_node_parent(self, node):
        if 'parent_id' not in node:
            return None
        parent_node = self.id_map.get(node['parent_id'])
        if not parent_node:
            return None
        return parent_node

    # -----
    # Record API
    # -----

    def save_data(self, data):
        if len(self.activated_group) > 0:
            # TODO use self.save_to_group_id
            target_group_id = list(self.activated_group.keys())[0]
            self.add_data(target_group_id, data)
        else:
            self.add_data('tmp_group', data)

    # -----
    # Editor
    # -----

    def update_group(self, _id, data, save=True):
        ignore_keys = ['id', 'parent_id', 'type', 'children']
        node = self.id_map.get(_id)
        if not node:
            raise IDNotFound(_id)

        update_data = {k: data[k] for k in data if k not in ignore_keys}
        node.update(update_data)

        delete_keys = [k for k in node if k not in data and k not in ignore_keys]
        for key in delete_keys:
            node.pop(key)

        # labels are ordered, sorted by label name
        if 'label' not in node:
            node['label'] = []
        elif 'label' in node and isinstance(node['label'], list):
            node['label'].sort(key=lambda x:x.get('name', '').lower())
        if save:
            self._adapter._update_group(node)

    def update_data(self, _id, data):
        node = self.id_map.get(_id)
        if not node:
            raise IDNotFound(_id)
        node['name'] = data['name']
        self._adapter._update_data(data)
        self._adapter._update_group(node)

    # -----
    # Snapshot
    # -----

    def export_from_local(self, event):
        group_id = self.import_from_local(event)
        filename = self.export_from_remote(group_id)
        return group_id, filename

    def export_from_remote(self, node_id):
        snapshot_path = self._get_snapshot_path()
        node = self.id_map.get(node_id)
        self._write_file(snapshot_path/PROP_FILE_NAME, node)

        ordered_data_id = self._collect_data_in_node(node)
        data_list = self._adapter._load_data_by_query({'id': ordered_data_id})
        for mock_data in data_list:
            self._write_file(snapshot_path/mock_data['id'], mock_data)

        filename = utils.compress_tar(snapshot_path, snapshot_path, suffix=self.SNAPSHOT_SUFFIX)
        self._remove_file([snapshot_path])
        return filename

    def import_from_local(self, event):
        _prop = event['snapshot']
        parent_path = config.get('snapshot.import.workspace', '/')
        parent_id = self.add_group_by_path(parent_path)

        new_data_map = {}
        for e in event['events']:
            data = self._make_data(e, data_id=e['id'])
            new_data_map[e['id']] = data

        self.import_(_prop, json=new_data_map)
        return self.paste(parent_id)

    def import_from_file(self, parent_id, input_path, **kwargs):
        snapshot_info, output_path = self.get_snapshot_file_detail(input_path)
        if kwargs.get('name'):
            snapshot_info['name'] = kwargs['name']

        self.import_(snapshot_info, path=output_path)
        _group_id = self.paste(parent_id=parent_id, path=output_path)
        self._remove_file([input_path, output_path])
        return _group_id

    def read_snapshot_from_link(self, link):
        snapshot_path = self._get_snapshot_path()
        snapshot_filename = Path(f'{snapshot_path}{self.SNAPSHOT_SUFFIX}')

        utils.download(link, snapshot_filename)
        return snapshot_filename

    def get_snapshot_file_detail(self, input_path):
        try:
            output_path = utils.decompress_tar(input_path)
        except Exception as e:
            raise LyrebirdSnapshotBroken(e)

        snapshot_prop = output_path / PROP_FILE_NAME

        if not snapshot_prop.exists():
            raise LyrebirdPropNotExists
        with codecs.open(str(snapshot_prop)) as f:
            snapshot_info = json.load(f)
        return snapshot_info, output_path

    def _write_file(self, path, data):
        data_str = json.dumps(data, ensure_ascii=False)
        with codecs.open(path, 'w') as f:
            f.write(data_str)

    def _remove_file(self, files):
        for filepath in files:
            path = Path(filepath)
            if path.is_dir() and path.exists():
                shutil.rmtree(path)
            elif path.is_file() and path.exists():
                path.unlink()

    def _get_snapshot_path(self):
        temp_dir_name = f"{time.strftime('%Y-%m-%d-%H-%M-%S', time.localtime())}-{str(uuid.uuid4())}"
        snapshot_path = self.snapshot_workspace / temp_dir_name
        snapshot_path.mkdir()
        return snapshot_path

# -----------------
# Exceptions
# -----------------

class DumpPropError(Exception):
    pass



class RootNotSet(Exception):
    pass


class RootPathNotExists(Exception):
    pass


class RootPathIsNotDir(Exception):
    pass


class LyrebirdPropNotExists(Exception):
    pass


class DataNotFound(Exception):
    pass


class DataObjectCannotContainAnyOtherObject(Exception):
    pass


class SuperIdCannotBeNodeItself(Exception):
    pass


class DataObjectSouldBeADict(Exception):
    pass


class IDNotFound(Exception):
    pass


class NoneClipbord(Exception):
    pass


class NonePropFile(Exception):
    pass


class TooMuchPropFile(Exception):
    pass


class NodeExist(Exception):
    pass


class LyrebirdSnapshotBroken(Exception):
    pass
