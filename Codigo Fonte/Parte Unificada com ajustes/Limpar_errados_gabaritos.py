#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Limpar_errados_gabarito.py
==========================

Limpeza de rótulos BenaPRO/BenaExt com apoio opcional do gabarito.

Uso mais simples, dentro da pasta dos JSONs:
    python .\Limpar_errados_gabarito.py

Uso recomendado:
    python .\Limpar_errados_gabarito.py --input-dir "." --output-dir ".\rotulos_limpos_gabarito" --reference ".\resultado - Meu.json" --mode conservative

O que este script faz:
1. Mantém o gabarito inalterado quando ele é usado como referência.
2. Corrige usuários com regras duras do manual.
3. Usa o gabarito para resolver o caso crítico:
   - Se o usuário marcou "Segmentação Boa" junto com erros:
     * se o gabarito é só "Segmentação Boa", mantém "Segmentação Boa" e remove os erros extras;
     * caso contrário, remove "Segmentação Boa" e mantém os erros.
4. Mantém "Dedo Fora da Área" grau 5 como exclusivo.
5. Resolve "Digital Clara" x "Digital Escura" usando o gabarito quando possível.
6. Salva os arquivos limpos com o MESMO nome original, dentro da pasta de saída.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SEGMENTACAO_BOA = "segmentacao boa"
DEDO_FORA_AREA = "dedo fora da area"
DIGITAL_CLARA = "digital clara"
DIGITAL_ESCURA = "digital escura"


DESCRICOES_PADRAO = {
    SEGMENTACAO_BOA: "A Segmentação ficou excelente, poucos erros aparentes.",
    DEDO_FORA_AREA: "Parte da digital ficou fora da área de captura.",
    DIGITAL_CLARA: "A digital está muito clara.",
    DIGITAL_ESCURA: "A digital está muito escura.",
}


IGNORED_JSON_NAMES = {
    "relatorio_limpeza.json",
    "resumo_limpeza.json",
}


def normalize_text(value: Any) -> str:
    """Normaliza texto para comparação robusta: sem acento, minúsculo e espaços simples."""
    if value is None:
        return ""
    text = str(value).strip().casefold()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"\s+", " ", text)
    return text


def label_key(error: Dict[str, Any]) -> str:
    key = normalize_text(error.get("nome"))
    # aliases comuns do projeto
    if key in {"scanner sujo", "escaner sujo", "escâner sujo"}:
        return "scanner sujo"
    if key == "dedo fora da área":
        return DEDO_FORA_AREA
    return key


def base_name(path: Any) -> str:
    return Path(str(path).replace("\\", "/")).name if path is not None else ""


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return default


def get_package_items(data: Any) -> Iterable[Tuple[str, List[Dict[str, Any]]]]:
    """Aceita JSON raiz como dict {pacote: [imagens]} ou lista direta."""
    if isinstance(data, dict):
        for package_name, images in data.items():
            if isinstance(images, list):
                yield str(package_name), images
    elif isinstance(data, list):
        yield "(lista)", data


def build_reference_index(reference_data: Any) -> Dict[str, Dict[str, Any]]:
    ref: Dict[str, Dict[str, Any]] = {}
    for _, images in get_package_items(reference_data):
        for item in images:
            if not isinstance(item, dict):
                continue
            name = base_name(item.get("arquivo"))
            if name and name not in ref:
                ref[name] = item
    return ref


def error_summary(errors: List[Dict[str, Any]]) -> str:
    parts = []
    for e in errors or []:
        nome = str(e.get("nome", ""))
        avaliacao = e.get("avaliacao", "")
        parts.append(f"{nome}:{avaliacao}")
    return "[" + "; ".join(parts) + "]"


def deduplicate_errors(errors: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Remove rótulos duplicados na mesma imagem, mantendo maior avaliação."""
    logs: List[str] = []
    best: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for err in errors:
        if not isinstance(err, dict):
            continue
        key = label_key(err)
        if not key:
            continue
        if key not in best:
            best[key] = err
            order.append(key)
            continue

        old = best[key]
        if safe_int(err.get("avaliacao"), -999) > safe_int(old.get("avaliacao"), -999):
            best[key] = err
        logs.append(f"Rótulo duplicado removido: {err.get('nome', key)}")

    return [best[k] for k in order if k in best], logs


def get_reference_keys(reference_entry: Optional[Dict[str, Any]]) -> set[str]:
    if not reference_entry:
        return set()
    errors = reference_entry.get("erros", [])
    if not isinstance(errors, list):
        return set()
    return {label_key(e) for e in errors if isinstance(e, dict) and label_key(e)}


def clean_image_entry(
    entry: Dict[str, Any],
    reference_entry: Optional[Dict[str, Any]] = None,
    mode: str = "conservative",
) -> Tuple[Dict[str, Any], List[str]]:
    """Limpa uma imagem individual."""
    cleaned = deepcopy(entry)
    raw_errors = cleaned.get("erros", [])
    if not isinstance(raw_errors, list):
        cleaned["erros"] = []
        return cleaned, ["Campo erros inválido; convertido para lista vazia"]

    errors, logs = deduplicate_errors(deepcopy(raw_errors))
    ref_keys = get_reference_keys(reference_entry)
    ref_is_only_good = ref_keys == {SEGMENTACAO_BOA}

    # 1) Dedo Fora da Área grau 5 é exclusivo.
    dedo_5 = [e for e in errors if label_key(e) == DEDO_FORA_AREA and safe_int(e.get("avaliacao")) == 5]
    if dedo_5:
        # Se houver mais de um, mantém o de maior avaliação/timestamp preservado pelo primeiro filtro.
        kept = dedo_5[0]
        if len(errors) != 1:
            logs.append("Dedo Fora da Área grau 5 é exclusivo; outros rótulos removidos")
        cleaned["erros"] = [kept]
        return cleaned, logs

    # 2) Segmentação Boa é exclusiva, mas agora a decisão usa o gabarito.
    keys = {label_key(e) for e in errors}
    if SEGMENTACAO_BOA in keys and len(errors) > 1:
        if ref_is_only_good:
            errors = [e for e in errors if label_key(e) == SEGMENTACAO_BOA]
            logs.append("Gabarito é Segmentação Boa; mantida Segmentação Boa e removidos erros extras")
        else:
            errors = [e for e in errors if label_key(e) != SEGMENTACAO_BOA]
            logs.append("Segmentação Boa removida porque havia E01-E08 na mesma imagem")

    # 3) Digital Clara e Digital Escura não coexistem.
    by_key = {label_key(e): e for e in errors}
    if DIGITAL_CLARA in by_key and DIGITAL_ESCURA in by_key:
        if DIGITAL_CLARA in ref_keys and DIGITAL_ESCURA not in ref_keys:
            errors = [e for e in errors if label_key(e) != DIGITAL_ESCURA]
            logs.append("Digital Clara x Digital Escura: mantida Digital Clara por concordar com o gabarito")
        elif DIGITAL_ESCURA in ref_keys and DIGITAL_CLARA not in ref_keys:
            errors = [e for e in errors if label_key(e) != DIGITAL_CLARA]
            logs.append("Digital Clara x Digital Escura: mantida Digital Escura por concordar com o gabarito")
        else:
            clara = by_key[DIGITAL_CLARA]
            escura = by_key[DIGITAL_ESCURA]
            av_clara = safe_int(clara.get("avaliacao"))
            av_escura = safe_int(escura.get("avaliacao"))
            if av_clara > av_escura:
                errors = [e for e in errors if label_key(e) != DIGITAL_ESCURA]
                logs.append("Digital Clara x Digital Escura: mantida Digital Clara por maior avaliação")
            elif av_escura > av_clara:
                errors = [e for e in errors if label_key(e) != DIGITAL_CLARA]
                logs.append("Digital Clara x Digital Escura: mantida Digital Escura por maior avaliação")
            else:
                errors = [e for e in errors if label_key(e) not in {DIGITAL_CLARA, DIGITAL_ESCURA}]
                logs.append("Digital Clara x Digital Escura: empate; ambos removidos para revisão")

    # 4) Modo strict: regras mais fortes, use só se quiser limpeza agressiva.
    if mode == "strict":
        by_key = {label_key(e): e for e in errors}
        # Dedo Fora da Área grau 4 como prioridade forte.
        if DEDO_FORA_AREA in by_key and safe_int(by_key[DEDO_FORA_AREA].get("avaliacao")) >= 4:
            kept = by_key[DEDO_FORA_AREA]
            if len(errors) != 1:
                logs.append("Modo strict: Dedo Fora da Área grau 4/5 mantido como prioritário")
            errors = [kept]
        else:
            by_key = {label_key(e): e for e in errors}
            # Sem Padrão Visível grau 5: mantém Sem Padrão e causa de contraste se existir.
            spv_key = "sem padrao visivel"
            if spv_key in by_key and safe_int(by_key[spv_key].get("avaliacao")) == 5:
                allowed = {spv_key, DIGITAL_CLARA, DIGITAL_ESCURA}
                new_errors = [e for e in errors if label_key(e) in allowed]
                if len(new_errors) != len(errors):
                    logs.append("Modo strict: Sem Padrão Visível grau 5 removeu rótulos secundários")
                errors = new_errors

    cleaned["erros"] = errors
    return cleaned, logs


def process_file(
    input_path: Path,
    output_dir: Path,
    mode: str,
    reference_index: Optional[Dict[str, Dict[str, Any]]] = None,
    reference_path: Optional[Path] = None,
) -> Dict[str, Any]:
    with input_path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)

    # O gabarito é copiado inalterado. Isso evita alterar a referência da análise.
    is_reference = reference_path is not None and input_path.resolve() == reference_path.resolve()
    cleaned_data = deepcopy(data)

    detailed_rows: List[Dict[str, Any]] = []
    total_images = 0
    changed_images = 0
    total_actions = 0

    if not is_reference:
        for package_name, images in get_package_items(cleaned_data):
            for idx, entry in enumerate(images):
                if not isinstance(entry, dict):
                    continue
                total_images += 1
                before = deepcopy(entry.get("erros", []))
                image_name = base_name(entry.get("arquivo"))
                ref_entry = reference_index.get(image_name) if reference_index else None
                cleaned_entry, logs = clean_image_entry(entry, ref_entry, mode)
                after = cleaned_entry.get("erros", [])
                images[idx] = cleaned_entry

                if before != after:
                    changed_images += 1
                    total_actions += len(logs)
                    detailed_rows.append({
                        "arquivo_json": input_path.name,
                        "pacote": package_name,
                        "imagem": str(entry.get("arquivo", "")),
                        "id": str(entry.get("id", "")),
                        "dedo": str(entry.get("dedo", "")),
                        "antes": error_summary(before),
                        "depois": error_summary(after),
                        "acoes": " | ".join(logs),
                    })
    else:
        for _, images in get_package_items(cleaned_data):
            total_images += len(images)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / input_path.name
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(cleaned_data, f, ensure_ascii=False, indent=2)

    return {
        "arquivo_json": input_path.name,
        "output_path": str(output_path),
        "total_imagens": total_images,
        "imagens_alteradas": changed_images,
        "total_acoes": total_actions,
        "detalhes": detailed_rows,
        "is_reference": is_reference,
    }


def find_json_files(input_dir: Path, output_dir: Path) -> List[Path]:
    files: List[Path] = []
    output_dir_resolved = output_dir.resolve()
    for p in sorted(input_dir.glob("*.json")):
        if p.name.casefold() in IGNORED_JSON_NAMES:
            continue
        try:
            if output_dir_resolved in p.resolve().parents:
                continue
        except Exception:
            pass
        files.append(p)
    return files


def auto_reference(input_dir: Path) -> Optional[Path]:
    preferred = input_dir / "resultado - Meu.json"
    if preferred.exists():
        return preferred.resolve()
    candidates = sorted(input_dir.glob("*Meu*.json"))
    return candidates[0].resolve() if candidates else None


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Limpa irregularidades dos JSONs BenaPRO/BenaExt.")
    parser.add_argument("files", nargs="*", help="Arquivos JSON específicos. Se vazio, usa todos os JSONs da pasta.")
    parser.add_argument("--input-dir", default=None, help="Pasta dos JSONs. Padrão: pasta do script.")
    parser.add_argument("--output-dir", default=None, help="Pasta de saída. Padrão: ./rotulos_limpos_gabarito.")
    parser.add_argument("--reference", default=None, help="JSON gabarito. Padrão: tenta achar 'resultado - Meu.json'.")
    parser.add_argument("--mode", choices=["conservative", "strict"], default="conservative", help="Modo de limpeza.")
    parser.add_argument("--backup", action="store_true", help="Cria .bak dos arquivos originais antes de processar.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    input_dir = Path(args.input_dir).expanduser().resolve() if args.input_dir else script_dir
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else input_dir / "rotulos_limpos_gabarito"

    if args.reference:
        reference_path = Path(args.reference).expanduser().resolve()
    else:
        reference_path = auto_reference(input_dir)

    reference_index: Optional[Dict[str, Dict[str, Any]]] = None
    if reference_path and reference_path.exists():
        with reference_path.open("r", encoding="utf-8-sig") as f:
            reference_data = json.load(f)
        reference_index = build_reference_index(reference_data)
    else:
        reference_path = None

    if args.files:
        input_files = [Path(f).expanduser().resolve() for f in args.files]
    else:
        input_files = find_json_files(input_dir, output_dir)

    if not input_files:
        print("Nenhum arquivo JSON encontrado.")
        print(f"Pasta verificada: {input_dir}")
        return 1

    print("=" * 78)
    print("LIMPEZA DE RÓTULOS BENAPRO - COM APOIO DO GABARITO")
    print("=" * 78)
    print(f"Pasta de entrada: {input_dir}")
    print(f"Pasta de saída:   {output_dir}")
    print(f"Modo:             {args.mode}")
    print(f"Gabarito:         {reference_path.name if reference_path else 'não encontrado; usando limpeza cega'}")
    print(f"Arquivos JSON:    {len(input_files)}")
    print("-" * 78)

    all_details: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []

    for input_path in input_files:
        if not input_path.exists():
            print(f"[ERRO] Arquivo não encontrado: {input_path}")
            continue
        if input_path.suffix.lower() != ".json":
            print(f"[PULADO] Não é JSON: {input_path.name}")
            continue
        if args.backup:
            shutil.copy2(input_path, input_path.with_suffix(input_path.suffix + ".bak"))

        try:
            result = process_file(input_path, output_dir, args.mode, reference_index, reference_path)
        except Exception as exc:
            print(f"[ERRO] Falha ao processar {input_path.name}: {exc}")
            continue

        all_details.extend(result["detalhes"])
        summary_rows.append({
            "arquivo_json": result["arquivo_json"],
            "tipo": "gabarito_copiado" if result["is_reference"] else "usuario_limpo",
            "total_imagens": result["total_imagens"],
            "imagens_alteradas": result["imagens_alteradas"],
            "percentual_alterado": (
                f"{(result['imagens_alteradas'] / result['total_imagens'] * 100):.2f}%"
                if result["total_imagens"] else "0.00%"
            ),
            "total_acoes": result["total_acoes"],
            "arquivo_saida": result["output_path"],
        })

        tag = "GABARITO" if result["is_reference"] else "OK"
        print(
            f"[{tag}] {input_path.name}: "
            f"{result['imagens_alteradas']}/{result['total_imagens']} imagens alteradas, "
            f"{result['total_acoes']} ação(ões)."
        )

    report_path = output_dir / "relatorio_limpeza.csv"
    summary_path = output_dir / "resumo_limpeza.csv"
    write_csv(report_path, all_details, ["arquivo_json", "pacote", "imagem", "id", "dedo", "antes", "depois", "acoes"])
    write_csv(summary_path, summary_rows, ["arquivo_json", "tipo", "total_imagens", "imagens_alteradas", "percentual_alterado", "total_acoes", "arquivo_saida"])

    print("-" * 78)
    print(f"Relatório detalhado: {report_path}")
    print(f"Resumo:              {summary_path}")
    print(f"Pasta de saída:      {output_dir}")
    print("Concluído.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
