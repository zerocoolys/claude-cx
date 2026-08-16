from __future__ import annotations

import json

from cx.model import REPLACE_WHOLE_KEYS, SCOPE_RANK, SourceFile


def merge_with_provenance(sources: list[SourceFile]) -> tuple[dict, dict]:
    """返回 (合并后配置, {点分路径: [(scope, path, value), ...]})。

    provenance 里保留全部贡献者，末位即为生效值。
    """
    merged: dict = {}
    prov: dict[str, list] = {}

    ordered = sorted(
        [s for s in sources if s.data],
        key=lambda s: (SCOPE_RANK[s.scope], sources.index(s)),
    )

    def walk(node: dict, into: dict, prefix: str, sf: SourceFile) -> None:
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and isinstance(into.get(k), dict):
                walk(v, into[k], path, sf)
                continue
            if isinstance(v, dict):
                into[k] = {}
                walk(v, into[k], path, sf)
                continue

            if isinstance(v, list) and path not in REPLACE_WHOLE_KEYS:
                # 数组拼接去重（permission 规则也走这条路，正是它们跨 scope 合并的原因）
                base = into.get(k, [])
                base = base if isinstance(base, list) else []
                seen = {json.dumps(x, sort_keys=True) for x in base}
                for item in v:
                    key = json.dumps(item, sort_keys=True)
                    if key not in seen:
                        base.append(item)
                        seen.add(key)
                into[k] = base
            else:
                into[k] = v

            prov.setdefault(path, []).append((sf.scope, sf.path, v))

    for sf in ordered:
        walk(sf.data, merged, "", sf)

    return merged, prov


def effective_scope(prov_entries: list) -> str:
    return prov_entries[-1][0] if prov_entries else "?"
