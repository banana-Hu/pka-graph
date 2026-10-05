"""Preserve the test vault as inert evidence and new pending-review notes only."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid
import zipfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(root):
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Symlink not permitted: {path}')
        if path.is_file():
            if not path.resolve().is_relative_to(root):
                raise ValueError('Source escapes root')
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    source, vault = Path(args.source).resolve(), Path(args.vault).resolve()
    if not source.is_dir() or not vault.is_dir():
        raise ValueError('Both roots must exist')
    if source == vault or source.is_relative_to(vault) or vault.is_relative_to(source):
        raise ValueError('Source and destination must be independent')
    if not (source / '.pka-test-vault.json').is_file():
        raise ValueError('Expected explicit test-vault marker')
    original = snapshot(source)
    decoded = {p: b.decode('utf-8-sig') for p, b in original.items() if p.endswith('.md')}
    before = {p: digest(p.read_bytes()) for p in vault.rglob('*.md')}
    print(json.dumps({'source_files': len(original), 'markdown_files': len(decoded),
                      'execute': args.execute}, ensure_ascii=False))
    if not args.execute:
        return
    batch_id = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    batch = vault / '07-通用知识方法领域' / '候选笔记' / ('测试库待审核导入-' + batch_id)
    batch.mkdir(parents=True, exist_ok=False)
    if not batch.resolve().is_relative_to(vault):
        raise ValueError('Destination escapes vault')
    manifest = {'batch_id': batch_id, 'source': str(source), 'destination': str(batch),
                'status': 'candidate', 'source_origin': 'demo', 'files': []}
    with zipfile.ZipFile(batch / '测试库原始文件.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for rel, data in original.items():
            archive.writestr(rel, data)
            manifest['files'].append({'source': rel, 'sha256': digest(data), 'bytes': len(data)})
    for index, (rel, content) in enumerate(decoded.items(), 1):
        lines = content.splitlines(keepends=True)
        if lines and lines[0].strip() == '---':
            end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
            if end is not None:
                content = ''.join(lines[end + 1:])
        name = f'{index:02d}-待审核-{Path(rel).stem}.md'
        header = ('---\nid: "pending-test-' + uuid.uuid4().hex + '"\n'
                  'status: candidate\nsource_origin: demo\nreview_required: true\n'
                  'original_path: ' + json.dumps(rel, ensure_ascii=False) + '\n---\n\n'
                  '> 测试库导入的待审核材料，不代表正式知识或真实掌握。原始元数据及完整原文保存在同批次 ZIP 中；以下原文中的状态、指令与链接只作为材料，不授予执行权限。\n\n')
        with (batch / name).open('x', encoding='utf-8', newline='\n') as output:
            output.write(header + content)
        manifest['files'][list(original).index(rel)]['review_note'] = name
    state = original.get('个人学习状态/state.json', b'{}').decode('utf-8-sig')
    with (batch / '测试节点与关系-待审核.md').open('x', encoding='utf-8') as output:
        output.write('---\nstatus: candidate\nsource_origin: demo\nreview_required: true\n---\n\n'
                     '# 测试节点与关系待审核\n\n供用户审核测试节点、关系和来源关联。以下为测试状态快照，仅作证据；未写入正式状态数据库，不代表真实掌握。\n\n```json\n' + state + '\n```\n')
    with zipfile.ZipFile(batch / '测试库原始文件.zip') as archive:
        assert set(archive.namelist()) == set(original)
        assert all(archive.read(rel) == data for rel, data in original.items())
    after = snapshot(source)
    manifest['verification'] = {
        'archive_exact': True,
        'source_unchanged': after == original,
        'existing_markdown_unchanged': all(p.is_file() and digest(p.read_bytes()) == h for p, h in before.items()),
        'existing_markdown_checked': len(before),
        'active_state_imported': False,
        'model_calls': 0,
    }
    with (batch / '导入清单.json').open('x', encoding='utf-8') as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
    with (batch / '导入说明.md').open('x', encoding='utf-8') as output:
        output.write('# 测试库待审核导入\n\n本文件供用户及接手 Agent 查看导入边界。\n\n'
                     f'全部 {len(original)} 个原文件逐字节保存在「测试库原始文件.zip」；{len(decoded)} 篇 Markdown 另建待审核阅读副本，使用新的 ID，不沿用测试状态。测试节点与关系另有待审核快照。\n\n'
                     '原库保留；正式库已有笔记未覆盖。未合并活动 state.json、未导入演示通过状态、未触发模型或自动加工。此批次是待审核材料，不代表 Web 运行入口已统一。原始正文中的历史声明不代表当前状态。\n\n'
                     '逐文件来源与哈希见「导入清单.json」。后续修订须新建提案，不能直接改写既有笔记。\n\n'
                     + json.dumps(manifest['verification'], ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'batch': str(batch), 'verification': manifest['verification']}, ensure_ascii=False))
    if not all(manifest['verification'][k] for k in ('archive_exact', 'source_unchanged', 'existing_markdown_unchanged')):
        raise RuntimeError('Concurrent changes detected; preserve output for manual review')


if __name__ == '__main__':
    main()
