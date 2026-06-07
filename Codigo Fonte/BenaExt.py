import os
import re
import io
import sys
import csv
import json
import copy
import time
import threading
import base64
import zipfile
import mimetypes
from collections import defaultdict, OrderedDict
from datetime import datetime
from pathlib import Path

Image = None
PIL_OK = None


def _ensure_pillow():
    """Carrega Pillow sob demanda para a janela abrir mais rapido."""
    global Image, PIL_OK
    if PIL_OK is not None:
        return PIL_OK
    try:
        from PIL import Image as PILImage
    except Exception:  # pragma: no cover
        PIL_OK = False
        return False
    Image = PILImage
    PIL_OK = True
    return True


APP_NAME = "BenaExt"
APP_VERSION = "1.0"
CONSENSUS_PASS_THRESHOLD = 0.5

if getattr(sys, "frozen", False):
    APP_DIR = getattr(
        sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    APP_RUN_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_RUN_DIR = APP_DIR

EXTRAS_DIR = os.path.join(APP_DIR, "Complementos")
SESSION_PATH = os.path.join(APP_RUN_DIR, "BenaExt Log.state")

EASTER_EGGS = {
    "678": {
        "name": "São Paulo",
        "image": "Escudo-São-Paulo.png",
        "audio": "hino_do_sao_paulo.wav",
    },
    "110": {
        "name": "Arsenal",
        "image": "Arsenal.png",
        "audio": "Hino_Arsenal.wav",
    },
    "83": {
        "name": "Gremio",
        "type": "gif",
        "image": "gremio.gif",
        "text": "VAMOO GR\u00caMIOO",
        "top_text": "VAMOO",
        "bottom_text": "GR\u00caMIOO",
    },
}

# Janela sempre em tela cheia e NAO redimensionavel. Se por algum motivo voce
# quiser apenas maximizar (com taskbar visivel) em vez de fullscreen real,
# troque para False.
FULLSCREEN_MODE = True

# Backend grafico preferido do pywebview.
# No codigo fonte, Qt e mantido pela estabilidade em fullscreen.
# No executavel Windows, Edge/WebView2 deixa o pacote menor e abre mais rapido.
GUI_BACKEND = (
    "edgechromium"
    if getattr(sys, "frozen", False) and sys.platform == "win32"
    else "qt"
)

# Dimensao maxima para exibicao das imagens (downscale para performance).
IMG_MAX_DIM = 1100
IMG_CACHE_LIMIT = 24
IMG_RENDER_CACHE_LIMIT = 48

# Placeholder neutro (1x1 transparente) usado quando nao ha imagem.
_BLANK_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

_QT_MESSAGE_HANDLER = None


# ---------------------------------------------------------------------------
# Utilidades gerais
# ---------------------------------------------------------------------------
def _install_qt_message_filter():
    """Oculta aviso inofensivo do Qt WebEngine no fechamento do app."""
    global _QT_MESSAGE_HANDLER
    if _QT_MESSAGE_HANDLER is not None:
        return
    try:
        from qtpy import QtCore
    except Exception:
        return

    ignored = (
        "Release of profile requested but WebEnginePage still not deleted",
        "QDxgiVSyncService not destroyed in time",
    )
    holder = {"previous": None}

    def handler(mode, context, message):
        text = str(message)
        if any(part in text for part in ignored):
            return
        previous = holder["previous"]
        if previous:
            try:
                previous(mode, context, message)
                return
            except Exception:
                pass
        try:
            sys.__stderr__.write(text + "\n")
            sys.__stderr__.flush()
        except Exception:
            pass

    holder["previous"] = QtCore.qInstallMessageHandler(handler)
    _QT_MESSAGE_HANDLER = handler

def base_name(path):
    """Nome do arquivo sem diretorio (aceita / e \\)."""
    if not path:
        return ""
    return os.path.basename(str(path).replace("\\", "/"))


def norm_key(name):
    """Chave normalizada para comparar nomes de erro (case/space-insensitive)."""
    if name is None:
        return ""
    return re.sub(r"\s+", " ", str(name).strip()).casefold()


def coerce_num(value):
    """Converte para float quando possivel; senao retorna None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            return float(str(value).replace(",", "."))
        except (ValueError, TypeError):
            return None


def derive_username(filename, fallback="usuario"):
    """
    Extrai um nome de usuario amigavel a partir do nome do arquivo JSON.
    Ex.: 'resultado - Brunao.json'         -> 'Brunao'
         'Resultado - Gabriel.json'        -> 'Gabriel'
         'resultado-gladson.json'          -> 'gladson'
         'Hospital - 12_2023 - Gabriel'    -> 'Gabriel'
         'resultado (1) - Reis.json'       -> 'Reis'
    """
    stem = os.path.splitext(base_name(filename))[0]
    raw = stem
    tokens = [t for t in re.split(r"[\s\-]+", stem) if t]
    stop = {"resultado", "resultados", "result", "hospital", "dados",
            "data", "final", "json", "arquivo", "labels", "rotulagem"}
    keep = []
    for t in tokens:
        low = t.casefold()
        if low in stop:
            continue
        if re.search(r"\d", t):            # descarta datas/numeros e (1),(2)
            continue
        if re.fullmatch(r"\(?\d+\)?", t):
            continue
        keep.append(t)
    if keep:
        # normalmente o nome da pessoa e o ultimo token relevante
        name = keep[-1].strip("()")
        if name:
            return name
    return raw or fallback


def safe_json_load(path):
    """Carrega JSON tolerando BOM e retorna (data, erro)."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "arquivo nao encontrado"
    except json.JSONDecodeError as exc:
        return None, "JSON malformado: %s" % exc
    except Exception as exc:  # pragma: no cover
        return None, "falha ao ler: %s" % exc


# ---------------------------------------------------------------------------
# Normalizacao de datasets
# ---------------------------------------------------------------------------
def normalize_errors(raw_errors):
    """Normaliza a lista de erros de uma imagem. Retorna (lista, avisos)."""
    out, warns = [], []
    if raw_errors is None:
        return out, warns
    if not isinstance(raw_errors, list):
        return out, ["campo 'erros' nao e uma lista"]
    for err in raw_errors:
        if not isinstance(err, dict):
            warns.append("erro em formato invalido (ignorado)")
            continue
        nome = err.get("nome")
        if nome is None or (isinstance(nome, str) and not nome.strip()):
            nome = "(sem nome)"
            warns.append("erro sem nome")
        nome = str(nome).strip()
        aval = coerce_num(err.get("avaliacao"))
        if err.get("avaliacao") in (None, "") and aval is None:
            warns.append("avaliacao ausente em '%s'" % nome)
        out.append({
            "nome": nome,
            "key": norm_key(nome),
            "descricao": err.get("descricao"),
            "avaliacao": aval,
            "timestamp": err.get("timestamp"),
        })
    return out, warns

def file_to_data_url(path):
    """Lê um arquivo local e devolve como data URL base64."""
    if not path or not os.path.exists(path):
        return None
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        raw = base64.b64encode(fh.read()).decode("ascii")
    return "data:%s;base64,%s" % (mime, raw)

def normalize_dataset(raw):
    """
    Converte o JSON cru em um indice por nome-base de arquivo.

    Retorna (images, warnings) onde images = {
        base_name: {arquivo, base, grupo, data, id, dedo, erros[], dupes}
    }
    """
    images = OrderedDict()
    warns = []
    if isinstance(raw, list):                 # estrutura alternativa: lista direta
        raw = {"(sem grupo)": raw}
        warns.append("raiz era uma lista; agrupada em '(sem grupo)'")
    if not isinstance(raw, dict):
        return images, ["estrutura raiz invalida (esperado objeto)"]

    for grupo, items in raw.items():
        if not isinstance(items, list):
            warns.append("grupo '%s' nao e uma lista (ignorado)" % grupo)
            continue
        for item in items:
            if not isinstance(item, dict):
                warns.append("item invalido em '%s' (ignorado)" % grupo)
                continue
            arq = item.get("arquivo")
            if not arq or not isinstance(arq, str):
                warns.append("item sem 'arquivo' em '%s' (ignorado)" % grupo)
                continue
            base = base_name(arq)
            erros, ew = normalize_errors(item.get("erros"))
            warns.extend(ew)
            rec = {
                "arquivo": arq,
                "base": base,
                "grupo": grupo,
                "data": item.get("data"),
                "id": item.get("id"),
                "dedo": item.get("dedo"),
                "erros": erros,
                "dupes": 0,
            }
            if base in images:
                images[base]["dupes"] += 1
                warns.append("arquivo duplicado: %s (mantido o primeiro)" % base)
                continue
            images[base] = rec
    return images, warns


# ---------------------------------------------------------------------------
# Engine de comparacao multilabel
# ---------------------------------------------------------------------------
def _error_keyset(record):
    """Conjunto de chaves de erro de um registro de imagem."""
    if not record:
        return set()
    return {e["key"] for e in record.get("erros", []) if e.get("key")}


def _display_map(record, into):
    for e in record.get("erros", []):
        into.setdefault(e["key"], e["nome"])


def _aval_map(record):
    out = {}
    for e in record.get("erros", []):
        if e["key"] not in out:
            out[e["key"]] = e.get("avaliacao")
    return out


def jaccard(a, b):
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


SEGMENTACAO_BOA = "segmentacao boa"
DEDO_FORA_AREA = "dedo fora da area"
DIGITAL_CLARA = "digital clara"
DIGITAL_ESCURA = "digital escura"


def _error_best_aval_map(record):
    out = {}
    if not record:
        return out
    for e in record.get("erros", []):
        key = e.get("key")
        if not key:
            continue
        aval = e.get("avaliacao")
        aval = coerce_num(aval)
        if key not in out:
            out[key] = aval
            continue
        prev = out[key]
        if aval is not None and (prev is None or aval > prev):
            out[key] = aval
    return out


def _majority_min_votes(total, threshold=CONSENSUS_PASS_THRESHOLD):
    if total <= 0:
        return 0
    return max(1, int((total * threshold) + 0.999999))


def _apply_majority_exclusion_rules(selected_keys, votes, aval5_votes, min_votes):
    selected = set(selected_keys)

    if aval5_votes.get(DEDO_FORA_AREA, 0) >= min_votes:
        return {DEDO_FORA_AREA}

    if SEGMENTACAO_BOA in selected and len(selected) > 1:
        return {SEGMENTACAO_BOA}

    if DIGITAL_CLARA in selected and DIGITAL_ESCURA in selected:
        clara_votes = votes.get(DIGITAL_CLARA, 0)
        escura_votes = votes.get(DIGITAL_ESCURA, 0)
        if clara_votes > escura_votes:
            selected.discard(DIGITAL_ESCURA)
        elif escura_votes > clara_votes:
            selected.discard(DIGITAL_CLARA)
        else:
            selected.discard(DIGITAL_CLARA)
            selected.discard(DIGITAL_ESCURA)

    return selected


def _build_label_consensus(orig_keys, user_records, display,
                           threshold=CONSENSUS_PASS_THRESHOLD):
    comparable_users = len(user_records)
    base = {
        "threshold": threshold,
        "score": None,
        "passed": None,
        "correct_votes": 0,
        "extra_votes": 0,
        "omitted_votes": 0,
        "denominator": 0,
        "total_user_votes": 0,
        "comparable_users": comparable_users,
        "majority_min_votes": 0,
        "majority_labels": [],
        "details": [],
    }
    if comparable_users <= 0:
        return base

    min_votes = _majority_min_votes(comparable_users, threshold)
    votes = defaultdict(int)
    aval5_votes = defaultdict(int)

    for rec in user_records.values():
        keys = _error_keyset(rec)
        avals = _error_best_aval_map(rec)

        for key in keys:
            votes[key] += 1

        dedo_aval = avals.get(DEDO_FORA_AREA)
        if dedo_aval is not None and dedo_aval >= 5:
            aval5_votes[DEDO_FORA_AREA] += 1

    raw_majority = {k for k, v in votes.items() if v >= min_votes}
    majority_keys = _apply_majority_exclusion_rules(
        raw_majority,
        votes,
        aval5_votes,
        min_votes,
    )

    correct_votes = sum(1 for k in majority_keys if k in orig_keys)
    extra_votes = sum(1 for k in majority_keys if k not in orig_keys)
    omitted_votes = sum(1 for k in orig_keys if k not in majority_keys)
    denominator = correct_votes + extra_votes + omitted_votes

    if denominator:
        score = correct_votes / float(denominator)
    elif not orig_keys:
        score = 1.0
    else:
        score = 0.0

    label_keys = sorted(
        set(orig_keys) | set(votes.keys()),
        key=lambda k: display.get(k, k).casefold(),
    )

    details = []
    for k in label_keys:
        vote_count = votes.get(k, 0)
        in_original = k in orig_keys
        reached_majority = vote_count >= min_votes
        survived_rules = k in majority_keys

        details.append({
            "key": k,
            "nome": display.get(k, k),
            "in_original": in_original,
            "votes": vote_count,
            "user_rate": round(vote_count / float(comparable_users), 4),
            "reached_majority": reached_majority,
            "survived_rules": survived_rules,
            "ignored_by_rule": reached_majority and not survived_rules,
            "status": (
                "correto" if survived_rules and in_original else
                "extra" if survived_rules and not in_original else
                "omitido" if (in_original and not survived_rules) else
                "ignorado"
            ),
            "missing_votes": (
                comparable_users - vote_count if in_original else 0
            ),
        })

    base.update({
        "score": round(score, 4),
        "passed": bool(score >= threshold),
        "correct_votes": correct_votes,
        "extra_votes": extra_votes,
        "omitted_votes": omitted_votes,
        "denominator": denominator,
        "total_user_votes": sum(votes.values()),
        "majority_min_votes": min_votes,
        "majority_labels": sorted(
            (display.get(k, k) for k in majority_keys),
            key=str.casefold,
        ),
        "details": details,
    })
    return base


def build_comparison(original, users):
    """
    Constroi a estrutura completa de comparacao.

    Parametros:
        original : images dict (gabarito)
        users    : {username: images dict}

    Retorna um dicionario serializavel em JSON com:
        - images: lista por imagem com comparacao por usuario
        - users : metricas agregadas por usuario
        - errors: estatisticas por tipo de erro
        - confusion: matriz de confusao simplificada
        - inter_user: concordancia entre usuarios
        - summary: totais gerais
        - error_labels: nomes de exibicao de cada erro
    """
    user_names = list(users.keys())

    # mapa de exibicao key -> nome legivel (preferencia: original, depois users)
    display = {}
    for rec in original.values():
        _display_map(rec, display)
    for udata in users.values():
        for rec in udata.values():
            _display_map(rec, display)

    # universo de imagens (uniao do gabarito + todos os usuarios)
    all_bases = list(original.keys())
    seen = set(all_bases)
    for udata in users.values():
        for b in udata.keys():
            if b not in seen:
                seen.add(b)
                all_bases.append(b)

    # acumuladores por usuario
    u_acc = {u: {"tp": 0, "fp": 0, "fn": 0, "exact": 0, "partial": 0,
                 "labeled": 0, "in_original": 0, "user_errors_total": 0,
                 "aval_abs_sum": 0.0, "aval_pairs": 0, "jacc_sum": 0.0}
             for u in user_names}

    # acumuladores por erro (somando todos os usuarios)
    err_acc = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0,
                                   "orig_count": 0, "user_count": 0})
    for rec in original.values():
        for k in _error_keyset(rec):
            err_acc[k]["orig_count"] += 1

    # matriz de confusao: orig label a -> user label b (em pares imagem/usuario)
    confusion = defaultdict(lambda: defaultdict(int))

    images_out = []
    total_original_errors = 0
    consensus_scored = 0
    consensus_passed = 0
    consensus_score_sum = 0.0
    consensus_vote_hits = 0
    consensus_vote_denominator = 0
    consensus_rows = []

    for base in all_bases:
        orig_rec = original.get(base)
        o_keys = _error_keyset(orig_rec)
        total_original_errors += len(o_keys)
        o_aval = _aval_map(orig_rec) if orig_rec else {}

        meta_src = orig_rec
        img_users = {}
        any_divergence = False
        all_full_agreement = bool(orig_rec)  # so faz sentido se ha gabarito
        comparable_users = 0
        user_keysets = {}
        user_records = {}

        for u in user_names:
            urec = users[u].get(base)
            if urec is None:
                img_users[u] = {"labeled": False}
                continue
            if meta_src is None:
                meta_src = urec
            comparable_users += 1
            u_keys = _error_keyset(urec)
            user_keysets[u] = u_keys
            user_records[u] = urec
            u_acc[u]["user_errors_total"] += len(u_keys)

            # so calcula metricas de acerto quando ha gabarito para a imagem
            if orig_rec is not None:
                tp = o_keys & u_keys
                fp = u_keys - o_keys
                fn = o_keys - u_keys
                exact = (o_keys == u_keys)
                partial = (not exact) and bool(tp)

                u_acc[u]["tp"] += len(tp)
                u_acc[u]["fp"] += len(fp)
                u_acc[u]["fn"] += len(fn)
                u_acc[u]["labeled"] += 1
                u_acc[u]["in_original"] += 1
                u_acc[u]["exact"] += 1 if exact else 0
                u_acc[u]["partial"] += 1 if partial else 0
                u_acc[u]["jacc_sum"] += jaccard(o_keys, u_keys)

                for k in tp:
                    err_acc[k]["tp"] += 1
                for k in fp:
                    err_acc[k]["fp"] += 1
                    err_acc[k]["user_count"] += 1
                for k in fn:
                    err_acc[k]["fn"] += 1
                for k in (tp | fp):
                    if k in tp:
                        err_acc[k]["user_count"] += 1

                # diferenca de avaliacao para erros em comum
                u_aval = _aval_map(urec)
                aval_diffs = []
                for k in tp:
                    ov, uv = o_aval.get(k), u_aval.get(k)
                    if ov is not None and uv is not None:
                        d = abs(ov - uv)
                        u_acc[u]["aval_abs_sum"] += d
                        u_acc[u]["aval_pairs"] += 1
                        aval_diffs.append({"erro": display.get(k, k),
                                           "original": ov, "usuario": uv,
                                           "diff": d})

                # matriz de confusao (substituicoes)
                for a in o_keys:
                    if a in u_keys:
                        confusion[a][a] += 1
                    else:
                        for b in fp:
                            confusion[a][b] += 1

                if not exact:
                    any_divergence = True
                else:
                    pass
                if not exact:
                    all_full_agreement = False

                img_users[u] = {
                    "labeled": True,
                    "erros": [{"nome": e["nome"], "descricao": e.get("descricao"),
                               "avaliacao": e.get("avaliacao"), "key": e["key"],
                               "status": ("tp" if e["key"] in tp else "fp")}
                              for e in urec["erros"]],
                    "tp": sorted(display.get(k, k) for k in tp),
                    "fp": sorted(display.get(k, k) for k in fp),
                    "fn": sorted(display.get(k, k) for k in fn),
                    "exact": exact,
                    "partial": partial,
                    "jaccard": round(jaccard(o_keys, u_keys), 4),
                    "aval_diffs": aval_diffs,
                }
            else:
                # imagem sem gabarito: guarda rotulagem mas nao pontua
                img_users[u] = {
                    "labeled": True,
                    "no_reference": True,
                    "erros": [{"nome": e["nome"], "descricao": e.get("descricao"),
                               "avaliacao": e.get("avaliacao"), "key": e["key"]}
                              for e in urec["erros"]],
                }

        # contagem de erros do usuario por tipo (independe de gabarito)
        for u in user_names:
            for k in user_keysets.get(u, set()):
                if orig_rec is None:
                    err_acc[k]["user_count"] += 1

        # concordancia entre usuarios nesta imagem (ignora gabarito)
        present = [user_keysets[u] for u in user_names if u in user_keysets]
        inter = _mean_pairwise_jaccard(present)
        label_consensus = None
        if orig_rec is not None:
            label_consensus = _build_label_consensus(
                o_keys, user_records, display)
            if label_consensus["score"] is not None:
                consensus_scored += 1
                consensus_score_sum += label_consensus["score"]
                consensus_passed += 1 if label_consensus["passed"] else 0
                consensus_vote_hits += label_consensus["correct_votes"]
                consensus_vote_denominator += label_consensus["denominator"]

        meta = {
            "arquivo": (meta_src or {}).get("arquivo", base),
            "data": (meta_src or {}).get("data"),
            "id": (meta_src or {}).get("id"),
            "dedo": (meta_src or {}).get("dedo"),
            "grupo": (meta_src or {}).get("grupo"),
        }
        images_out.append({
            "base": base,
            "meta": meta,
            "has_reference": orig_rec is not None,
            "original_erros": [{"nome": e["nome"], "descricao": e.get("descricao"),
                                "avaliacao": e.get("avaliacao"), "key": e["key"]}
                               for e in (orig_rec or {}).get("erros", [])],
            "users": img_users,
            "divergence": any_divergence,
            "full_agreement": all_full_agreement and comparable_users > 0,
            "comparable_users": comparable_users,
            "inter_user_agreement": round(inter, 4) if inter is not None else None,
            "label_consensus": label_consensus,
            "dupes": (orig_rec or {}).get("dupes", 0),
        })
        if label_consensus and label_consensus["score"] is not None:
            consensus_rows.append({
                "base": base,
                "arquivo": meta["arquivo"],
                "score": label_consensus["score"],
                "passed": label_consensus["passed"],
                "correct_votes": label_consensus["correct_votes"],
                "extra_votes": label_consensus["extra_votes"],
                "omitted_votes": label_consensus["omitted_votes"],
                "denominator": label_consensus["denominator"],
                "total_user_votes": label_consensus["total_user_votes"],
                "comparable_users": label_consensus["comparable_users"],
            })

    # ---- metricas agregadas por usuario ----
    users_out = []
    for u in user_names:
        a = u_acc[u]
        tp, fp, fn = a["tp"], a["fp"], a["fn"]
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if (precision + recall) else 0.0)
        jacc = tp / (tp + fp + fn) if (tp + fp + fn) else 0.0
        labeled = a["in_original"]
        exact_rate = a["exact"] / labeled if labeled else 0.0
        partial_rate = a["partial"] / labeled if labeled else 0.0
        mean_jacc_img = a["jacc_sum"] / labeled if labeled else 0.0
        aval_mae = (a["aval_abs_sum"] / a["aval_pairs"]
                    if a["aval_pairs"] else None)
        users_out.append({
            "usuario": u,
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "jaccard": round(jacc, 4),
            "exact": a["exact"],
            "partial": a["partial"],
            "exact_rate": round(exact_rate, 4),
            "partial_rate": round(partial_rate, 4),
            "mean_img_jaccard": round(mean_jacc_img, 4),
            "labeled_images": labeled,
            "user_errors_total": a["user_errors_total"],
            "aval_mae": (round(aval_mae, 3) if aval_mae is not None else None),
            "aval_pairs": a["aval_pairs"],
        })

    # ranking por proximidade ao gabarito (F1, desempate exact_rate)
    ranking = sorted(users_out, key=lambda x: (x["f1"], x["exact_rate"]),
                     reverse=True)
    rank_names = [r["usuario"] for r in ranking]

    # ---- estatisticas por erro ----
    errors_out = []
    for k, a in err_acc.items():
        tp, fp, fn = a["tp"], a["fp"], a["fn"]
        denom = tp + fp + fn
        agree = tp / denom if denom else 0.0
        errors_out.append({
            "key": k,
            "nome": display.get(k, k),
            "tp": tp, "fp": fp, "fn": fn,
            "orig_count": a["orig_count"],
            "user_count": a["user_count"],
            "agreement": round(agree, 4),
            "divergence": round(1 - agree, 4),
        })
    errors_out.sort(key=lambda x: x["nome"].casefold())

    most_omitted = sorted(errors_out, key=lambda x: x["fn"], reverse=True)
    most_extra = sorted(errors_out, key=lambda x: x["fp"], reverse=True)
    sup = [e for e in errors_out if (e["tp"] + e["fp"] + e["fn"]) > 0]
    best_agree = sorted(sup, key=lambda x: x["agreement"], reverse=True)
    worst_agree = sorted(sup, key=lambda x: x["agreement"])

    # ---- matriz de confusao ----
    labels_sorted = sorted(display.keys(), key=lambda k: display[k].casefold())
    conf_matrix = []
    for a in labels_sorted:
        row = [confusion[a][b] for b in labels_sorted]
        conf_matrix.append(row)
    confusion_out = {
        "labels": [display.get(k, k) for k in labels_sorted],
        "matrix": conf_matrix,
    }

    # ---- concordancia entre usuarios (matriz par a par) ----
    inter_matrix = []
    for u1 in user_names:
        row = []
        for u2 in user_names:
            if u1 == u2:
                row.append(1.0)
                continue
            vals = []
            for base in all_bases:
                r1 = users[u1].get(base)
                r2 = users[u2].get(base)
                if r1 is not None and r2 is not None:
                    vals.append(jaccard(_error_keyset(r1), _error_keyset(r2)))
            row.append(round(sum(vals) / len(vals), 4) if vals else None)
        inter_matrix.append(row)

    # imagens com maior divergencia entre usuarios
    div_imgs = [im for im in images_out
                if im["inter_user_agreement"] is not None
                and im["comparable_users"] >= 2]
    div_imgs_sorted = sorted(
        div_imgs, key=lambda im: im["inter_user_agreement"])
    images_most_divergent = [{
        "base": im["base"],
        "arquivo": im["meta"]["arquivo"],
        "inter_user_agreement": im["inter_user_agreement"],
        "comparable_users": im["comparable_users"],
    } for im in div_imgs_sorted[:25]]

    # distribuicao das avaliacoes (gabarito x usuarios)
    aval_orig = defaultdict(int)
    aval_user = defaultdict(int)
    for rec in original.values():
        for e in rec["erros"]:
            if e.get("avaliacao") is not None:
                aval_orig[int(round(e["avaliacao"]))] += 1
    for udata in users.values():
        for rec in udata.values():
            for e in rec["erros"]:
                if e.get("avaliacao") is not None:
                    aval_user[int(round(e["avaliacao"]))] += 1
    keys = list(aval_orig) + list(aval_user)
    lo = min(keys + [1]) if keys else 1
    hi = max(keys + [5]) if keys else 5
    aval_labels = list(range(lo, hi + 1))
    aval_distribution = {
        "labels": aval_labels,
        "original": [aval_orig.get(k, 0) for k in aval_labels],
        "users": [aval_user.get(k, 0) for k in aval_labels],
    }

    summary = {
        "total_images": len(images_out),
        "total_images_with_reference": sum(1 for im in images_out
                                           if im["has_reference"]),
        "total_users": len(user_names),
        "total_original_errors": total_original_errors,
        "total_divergent_images": sum(1 for im in images_out
                                      if im["divergence"]),
        "total_full_agreement_images": sum(1 for im in images_out
                                           if im["full_agreement"]),
        "error_types": len(labels_sorted),
        "consensus_threshold": CONSENSUS_PASS_THRESHOLD,
        "consensus_scored_images": consensus_scored,
        "consensus_passed_images": consensus_passed,
        "consensus_failed_images": consensus_scored - consensus_passed,
        "consensus_pass_rate": (round(consensus_passed / consensus_scored, 4)
                                if consensus_scored else None),
        "consensus_mean_score": (round(consensus_score_sum / consensus_scored, 4)
                                 if consensus_scored else None),
        "consensus_vote_hits": consensus_vote_hits,
        "consensus_vote_denominator": consensus_vote_denominator,
        "consensus_global_score": (
            round(consensus_vote_hits / consensus_vote_denominator, 4)
            if consensus_vote_denominator else None),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    consensus_rows.sort(key=lambda x: (x["score"], x["arquivo"] or x["base"]))
    failed_consensus = [x for x in consensus_rows if not x["passed"]]
    passed_consensus = sorted(
        (x for x in consensus_rows if x["passed"]),
        key=lambda x: (-x["score"], x["arquivo"] or x["base"]))

    return {
        "summary": summary,
        "images": images_out,
        "users": users_out,
        "ranking": rank_names,
        "errors": errors_out,
        "most_omitted": most_omitted[:15],
        "most_extra": most_extra[:15],
        "best_agreement": best_agree[:15],
        "worst_agreement": worst_agree[:15],
        "confusion": confusion_out,
        "inter_user": {"users": user_names, "matrix": inter_matrix},
        "images_most_divergent": images_most_divergent,
        "images_low_consensus": consensus_rows[:25],
        "images_failed_consensus": failed_consensus[:25],
        "images_passed_consensus": passed_consensus[:25],
        "aval_distribution": aval_distribution,
        "error_labels": [display.get(k, k) for k in labels_sorted],
    }


def build_zip_preview_comparison(zip_index):
    """Estrutura minima para navegar imagens quando so o ZIP foi carregado."""
    images_out = []
    for base, internal_name in zip_index.items():
        images_out.append({
            "base": base,
            "meta": {
                "arquivo": internal_name,
                "data": None,
                "id": None,
                "dedo": None,
                "grupo": None,
            },
            "has_reference": False,
            "original_erros": [],
            "users": {},
            "divergence": False,
            "full_agreement": False,
            "comparable_users": 0,
            "inter_user_agreement": None,
            "label_consensus": None,
            "dupes": 0,
            "in_zip": True,
        })

    return {
        "summary": {
            "total_images": len(images_out),
            "total_images_with_reference": 0,
            "total_users": 0,
            "total_original_errors": 0,
            "total_divergent_images": 0,
            "total_full_agreement_images": 0,
            "error_types": 0,
            "consensus_threshold": CONSENSUS_PASS_THRESHOLD,
            "consensus_scored_images": 0,
            "consensus_passed_images": 0,
            "consensus_failed_images": 0,
            "consensus_pass_rate": None,
            "consensus_mean_score": None,
            "consensus_vote_hits": 0,
            "consensus_vote_denominator": 0,
            "consensus_global_score": None,
            "zip_only_count": len(images_out),
            "json_not_in_zip": 0,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "images": images_out,
        "users": [],
        "ranking": [],
        "errors": [],
        "most_omitted": [],
        "most_extra": [],
        "best_agreement": [],
        "worst_agreement": [],
        "confusion": {"labels": [], "matrix": []},
        "inter_user": {"users": [], "matrix": []},
        "images_most_divergent": [],
        "images_low_consensus": [],
        "images_failed_consensus": [],
        "images_passed_consensus": [],
        "aval_distribution": {
            "labels": [1, 2, 3, 4, 5],
            "original": [0, 0, 0, 0, 0],
            "users": [0, 0, 0, 0, 0],
        },
        "zip_only": sorted(zip_index),
        "error_labels": [],
    }


def _mean_pairwise_jaccard(keysets):
    """Media de Jaccard par a par entre conjuntos. None se < 2 conjuntos."""
    n = len(keysets)
    if n < 2:
        return None
    total, pairs = 0.0, 0
    for i in range(n):
        for j in range(i + 1, n):
            total += jaccard(keysets[i], keysets[j])
            pairs += 1
    return total / pairs if pairs else None


# ---------------------------------------------------------------------------
# Camada de imagem (ZIP -> base64)
# ---------------------------------------------------------------------------
class ImageStore:
    """Indexa um ZIP por nome-base e renderiza imagens/canais em base64."""

    def __init__(self):
        self.zip_path = None
        self.index = {}          # base_name -> caminho interno no zip
        self.duplicates = []     # nomes base duplicados no zip
        self._cache = OrderedDict()  # base -> PIL RGBA (downscaled)
        self._render_cache = OrderedDict()  # (base, mode) -> data URL pronta

    def load(self, zip_path):
        self.zip_path = zip_path
        self.index = {}
        self.duplicates = []
        self._cache.clear()
        self._render_cache.clear()
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if not name.lower().endswith(
                        (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif",
                         ".tiff")):
                    continue
                base = base_name(name)
                if base in self.index:
                    self.duplicates.append(base)
                    continue
                self.index[base] = name
        return len(self.index)

    def has(self, base):
        return base in self.index

    def _get_rgba(self, base):
        if base in self._cache:
            self._cache.move_to_end(base)
            return self._cache[base]
        if not self.zip_path or base not in self.index:
            return None
        with zipfile.ZipFile(self.zip_path) as zf:
            data = zf.read(self.index[base])
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGBA")
        img.thumbnail((IMG_MAX_DIM, IMG_MAX_DIM), Image.LANCZOS)
        self._cache[base] = img
        self._cache.move_to_end(base)
        while len(self._cache) > IMG_CACHE_LIMIT:
            self._cache.popitem(last=False)
        return img

    def render(self, base, mode="rgba"):
        """Retorna dict {ok, data(base64), w, h, mode}."""
        if not _ensure_pillow():
            return {"ok": False, "error": "Pillow nao instalado"}
        mode = (mode or "rgba").lower()
        cache_key = (base, mode)
        if cache_key in self._render_cache:
            self._render_cache.move_to_end(cache_key)
            return dict(self._render_cache[cache_key])
        img = self._get_rgba(base)
        if img is None:
            return {"ok": False, "error": "imagem ausente no ZIP"}
        w, h = img.size
        if mode == "rgba":
            out = img
        elif mode == "rgb":
            out = img.convert("RGB")
        elif mode == "alpha":
            out = img.getchannel("A")
        elif mode == "r":
            out = img.getchannel("R")
        elif mode == "g":
            out = img.getchannel("G")
        elif mode == "b":
            out = img.getchannel("B")
        else:
            out = img
        buff = io.BytesIO()
        out.save(buff, format="PNG")
        b64 = base64.b64encode(buff.getvalue()).decode("ascii")
        result = {"ok": True, "data": "data:image/png;base64," + b64,
                  "w": w, "h": h, "mode": mode}
        self._render_cache[cache_key] = result
        self._render_cache.move_to_end(cache_key)
        while len(self._render_cache) > IMG_RENDER_CACHE_LIMIT:
            self._render_cache.popitem(last=False)
        return dict(result)


# ---------------------------------------------------------------------------
# API exposta ao front-end (pywebview js_api)
# ---------------------------------------------------------------------------
def _fd(kind):
    """Constante de dialogo compativel com versoes novas/antigas do pywebview.
    Usa o enum FileDialog quando existir (evita avisos de depreciacao)."""
    import webview
    FD = getattr(webview, "FileDialog", None)
    if FD is not None:
        return {"open": FD.OPEN, "folder": FD.FOLDER, "save": FD.SAVE}[kind]
    return {"open": webview.OPEN_DIALOG, "folder": webview.FOLDER_DIALOG,
            "save": webview.SAVE_DIALOG}[kind]


class Api:
    def __init__(self):
        self.window = None
        self.restore_fullscreen_callback = None
        self.fullscreen_restore_pending = False
        self.original_path = None
        self.users_folder = None
        self.original = OrderedDict()
        self.users = OrderedDict()        # username -> images dict
        self.user_files = OrderedDict()   # username -> path
        self.store = ImageStore()
        self.comparison = None
        self.warnings = []
        self.session_save_paused = False

    # ----- estado / status -----
    def get_status(self):
        return {
            "ok": True,
            "app": APP_NAME, "version": APP_VERSION,
            "original_path": self.original_path,
            "original_loaded": bool(self.original),
            "original_images": len(self.original),
            "users_folder": self.users_folder,
            "users": list(self.users.keys()),
            "users_count": len(self.users),
            "zip_path": self.store.zip_path,
            "zip_images": len(self.store.index),
            "saved_session": os.path.exists(SESSION_PATH),
            "warnings": self.warnings[-200:],
        }

    # ----- sessao anterior -----
    def _session_payload(self):
        def abspath_or_none(path):
            return os.path.abspath(path) if path else None

        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "original_path": abspath_or_none(self.original_path),
            "users_folder": abspath_or_none(self.users_folder),
            "zip_path": abspath_or_none(self.store.zip_path),
        }

    def _save_session(self):
        if self.session_save_paused:
            return
        try:
            tmp_path = SESSION_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._session_payload(), fh,
                          ensure_ascii=False, indent=2)
            os.replace(tmp_path, SESSION_PATH)
        except Exception as exc:
            self.warnings.append(
                "[sessao] falha ao salvar ultima sessao: " + _fmt_exc(exc))

    def _read_saved_session(self):
        data, err = safe_json_load(SESSION_PATH)
        if err:
            return None, "Nenhuma sessao anterior salva"
        if not isinstance(data, dict):
            return None, "Sessao anterior em formato invalido"
        return data, None

    def _clear_loaded_data(self):
        self.original_path = None
        self.users_folder = None
        self.original = OrderedDict()
        self.users = OrderedDict()
        self.user_files = OrderedDict()
        self.store = ImageStore()
        self.comparison = None
        self.warnings = []

    def restore_last_session(self):
        data, err = self._read_saved_session()
        if err:
            return {"ok": False, "error": err}

        original_path = data.get("original_path")
        users_folder = data.get("users_folder")
        zip_path = data.get("zip_path")
        if not any((original_path, users_folder, zip_path)):
            return {"ok": False, "error": "Sessao anterior vazia"}

        self._clear_loaded_data()
        self.session_save_paused = True
        loaded = {}
        problems = []
        try:
            if original_path:
                if os.path.isfile(original_path):
                    r = self.load_original(original_path)
                    if r.get("ok"):
                        loaded["original_images"] = r.get("images", 0)
                    else:
                        problems.append("original: " + r.get("error", "?"))
                else:
                    problems.append("original nao encontrado: " + original_path)

            if users_folder:
                if os.path.isdir(users_folder):
                    r = self.load_users_folder(users_folder)
                    if r.get("ok"):
                        loaded["users_count"] = len(r.get("users", []))
                        problems.extend(r.get("problems", []))
                    else:
                        problems.append("usuarios: " + r.get("error", "?"))
                else:
                    problems.append("pasta de usuarios nao encontrada: " +
                                    users_folder)

            if zip_path:
                if os.path.isfile(zip_path):
                    r = self.load_zip(zip_path)
                    if r.get("ok"):
                        loaded["zip_images"] = r.get("images", 0)
                    else:
                        problems.append("zip: " + r.get("error", "?"))
                else:
                    problems.append("zip nao encontrado: " + zip_path)
        finally:
            self.session_save_paused = False

        self._recompute()
        if problems:
            self.warnings += ["[sessao] " + p for p in problems]
        if loaded:
            self._save_session()
            return {"ok": True, "loaded": loaded, "problems": problems,
                    "status": self.get_status()}
        return {"ok": False,
                "error": "Nao consegui carregar nada da sessao anterior",
                "problems": problems}

    # ----- dialogos de arquivo -----
    def _dialog(self, dialog_type, directory="", file_types=None,
                allow_multiple=False):
        kwargs = {"allow_multiple": allow_multiple}
        if directory:
            kwargs["directory"] = directory
        if file_types:
            kwargs["file_types"] = file_types
        result = self.window.create_file_dialog(dialog_type, **kwargs)
        return result

    def pick_original(self):
        try:
            res = self._dialog(_fd("open"),
                               file_types=("JSON (*.json)", "Todos (*.*)"))
            if not res:
                return {"ok": False, "cancelled": True}
            path = res[0]
            return self.load_original(path)
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}

    def pick_users_folder(self):
        try:
            res = self._dialog(_fd("folder"))
            if not res:
                return {"ok": False, "cancelled": True}
            folder = res[0]
            return self.load_users_folder(folder)
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}

    def pick_zip(self):
        try:
            res = self._dialog(_fd("open"),
                               file_types=("ZIP (*.zip)", "Todos (*.*)"))
            if not res:
                return {"ok": False, "cancelled": True}
            return self.load_zip(res[0])
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}

    # ----- carregamento -----
    def load_original(self, path):
        data, err = safe_json_load(path)
        if err:
            return {"ok": False, "error": "Original: " + err}
        images, warns = normalize_dataset(data)
        if not images:
            return {"ok": False,
                    "error": "Nenhuma imagem valida no JSON original"}
        self.original_path = path
        self.original = images
        self.warnings += ["[original] " + w for w in warns]
        self._recompute()
        self._save_session()
        return {"ok": True, "images": len(images),
                "name": base_name(path), "warnings": warns}

    def load_users_folder(self, folder):
        if not os.path.isdir(folder):
            return {"ok": False, "error": "pasta invalida"}
        self.users_folder = folder
        self.users = OrderedDict()
        self.user_files = OrderedDict()
        loaded, problems = [], []
        files = sorted(f for f in os.listdir(folder)
                       if f.lower().endswith(".json"))
        used_names = set()
        for fname in files:
            full = os.path.join(folder, fname)
            if self.original_path and os.path.abspath(full) == \
                    os.path.abspath(self.original_path):
                continue  # nao incluir o proprio gabarito
            data, err = safe_json_load(full)
            if err:
                problems.append("%s: %s" % (fname, err))
                continue
            images, warns = normalize_dataset(data)
            if not images:
                problems.append("%s: Sem imagens validas" % fname)
                continue
            name = derive_username(fname)
            base = name
            i = 2
            while name in used_names:
                name = "%s (%d)" % (base, i)
                i += 1
            used_names.add(name)
            self.users[name] = images
            self.user_files[name] = full
            loaded.append({"usuario": name, "arquivo": fname,
                           "images": len(images)})
            self.warnings += ["[%s] %s" % (name, w) for w in warns]
        self._recompute()
        self._save_session()
        return {"ok": True, "loaded": loaded, "problems": problems,
                "users": list(self.users.keys())}

    def load_zip(self, path):
        try:
            n = self.store.load(path)
        except zipfile.BadZipFile:
            return {"ok": False, "error": "ZIP invalido/corrompido"}
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}
        if self.store.duplicates:
            self.warnings.append("[zip] %d nome(s) duplicado(s) no ZIP" %
                                 len(set(self.store.duplicates)))
        self._recompute()
        self._save_session()
        return {"ok": True, "images": n, "name": base_name(path),
                "duplicates": sorted(set(self.store.duplicates))}

    def _recompute(self):
        if self.original or self.users:
            self.comparison = build_comparison(self.original, self.users)
            # enriquecer imagens com flag de presenca no ZIP
            for im in self.comparison["images"]:
                im["in_zip"] = self.store.has(im["base"])
            # imagens no ZIP que nao estao em nenhum JSON
            json_bases = {im["base"] for im in self.comparison["images"]}
            zip_only = [b for b in self.store.index if b not in json_bases]
            self.comparison["zip_only"] = sorted(zip_only)
            self.comparison["summary"]["zip_only_count"] = len(zip_only)
            self.comparison["summary"]["json_not_in_zip"] = sum(
                1 for im in self.comparison["images"] if not im["in_zip"])
        elif self.store.index:
            self.comparison = build_zip_preview_comparison(self.store.index)
        else:
            self.comparison = None

    # ----- dados para o front-end -----
    def get_comparison(self):
        if self.comparison is None:
            return {"ok": False, "error": "carregue o ZIP ou o JSON original"}
        return {"ok": True, "data": self.comparison, "status": self.get_status()}

    def get_image(self, base, mode="rgba"):
        return self.store.render(base, mode)

    def get_easter_assets(self, code):
        try:
            code = str(code)
            egg = EASTER_EGGS.get(code)

            if not egg:
                return {
                    "ok": False,
                    "error": "Código de easter egg inválido: %s" % code
                }

            payload = {
                "ok": True,
                "code": code,
                "name": egg["name"],
                "type": egg.get("type", "image"),
                "text": egg.get("text"),
                "top_text": egg.get("top_text"),
                "bottom_text": egg.get("bottom_text"),
            }

            if egg.get("image"):
                image_path = os.path.join(EXTRAS_DIR, egg["image"])
                if not os.path.exists(image_path):
                    return {
                        "ok": False,
                        "error": "Imagem do easter egg não encontrada: %s" % image_path
                    }
                image_data = file_to_data_url(image_path)
                if not image_data:
                    return {
                        "ok": False,
                        "error": "Falha ao carregar a imagem do easter egg"
                    }
                payload["image"] = image_data

            if egg.get("audio"):
                audio_path = os.path.join(EXTRAS_DIR, egg["audio"])
                if not os.path.exists(audio_path):
                    return {
                        "ok": False,
                        "error": "Áudio do easter egg não encontrado: %s" % audio_path
                    }
                audio_data = file_to_data_url(audio_path)
                if not audio_data:
                    return {
                        "ok": False,
                        "error": "Falha ao carregar o áudio do easter egg"
                    }
                payload["audio"] = audio_data

            if egg.get("video"):
                video_path = os.path.join(EXTRAS_DIR, egg["video"])
                if not os.path.exists(video_path):
                    return {
                        "ok": False,
                        "error": "Video do easter egg nao encontrado: %s" % video_path
                    }
                video_data = file_to_data_url(video_path)
                if not video_data:
                    return {
                        "ok": False,
                        "error": "Falha ao carregar o video do easter egg"
                    }
                payload["video"] = video_data
                payload["video_url"] = Path(video_path).resolve().as_uri()

            if not any(k in payload for k in ("image", "audio", "video")):
                return {"ok": False, "error": "Easter egg sem asset configurado"}

            return payload
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}

    def refresh(self):
        """Recarrega original + pasta de usuarios + ZIP a partir dos caminhos."""
        msgs = []
        self.warnings = []
        if self.original_path and os.path.exists(self.original_path):
            r = self.load_original(self.original_path)
            if not r.get("ok"):
                msgs.append("original: " + r.get("error", "?"))
        if self.users_folder and os.path.isdir(self.users_folder):
            self.load_users_folder(self.users_folder)
        if self.store.zip_path and os.path.exists(self.store.zip_path):
            try:
                self.store.load(self.store.zip_path)
            except Exception as exc:
                msgs.append("zip: " + _fmt_exc(exc))
        self._recompute()
        return {"ok": True, "messages": msgs, "status": self.get_status()}

    # ----- controles de janela -----
    def win_minimize(self):
        try:
            if FULLSCREEN_MODE:
                self.fullscreen_restore_pending = True
            self.window.minimize()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}

    def win_restore_fullscreen(self):
        try:
            if not (FULLSCREEN_MODE and self.fullscreen_restore_pending):
                return {"ok": True, "skipped": True}
            if self.restore_fullscreen_callback:
                ok = bool(self.restore_fullscreen_callback())
            else:
                ok = False
            self.fullscreen_restore_pending = not ok
            return {"ok": ok}
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}

    def win_close(self):
        _force_process_exit(0)

    # ----- exportacoes -----
    def _save_dialog(self, filename, file_types):
        res = self.window.create_file_dialog(
            _fd("save"), save_filename=filename, file_types=file_types)
        if not res:
            return None
        return res if isinstance(res, str) else res[0]

    def export_csv(self):
        if not self.comparison:
            return {"ok": False, "error": "Não há nada para exportar"}
        try:
            path = self._save_dialog("BenaExt Resultados.csv",
                                     ("CSV (*.csv)",))
            if not path:
                return {"ok": False, "cancelled": True}
            rows = _build_csv_rows(self.comparison)
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys())
                                        if rows else ["info"])
                writer.writeheader()
                for r in rows:
                    writer.writerow(r)
            return {"ok": True, "path": path, "rows": len(rows)}
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}

    def _export_status(self, anonymize=False):
        status = self.get_status()
        if anonymize and self.comparison:
            aliases = _user_alias_map(self.comparison)
            status = copy.deepcopy(status)
            status["users"] = [aliases.get(u, u) for u in self.users.keys()]
            status["users_folder"] = None
            status["warnings"] = [_replace_user_tokens(w, aliases)
                                  for w in status.get("warnings", [])]
        return status

    def export_json(self, charts=None, anonymize=False):
        if not self.comparison:
            return {"ok": False, "error": "Não há nada para exportar"}
        try:
            path = self._save_dialog("BenaExt Comparacao.json",
                                     ("JSON (*.json)",))
            if not path:
                return {"ok": False, "cancelled": True}
            comp = _anonymized_comparison(self.comparison) if anonymize else self.comparison
            payload = {
                "app": APP_NAME, "version": APP_VERSION,
                "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "anonimizado": bool(anonymize),
                "original": self.original_path,
                "usuarios": [u["usuario"] for u in comp.get("users", [])],
                "zip": self.store.zip_path,
                "status": self._export_status(anonymize),
                "consenso_rotulos": {
                    "threshold": comp.get("summary", {}).get(
                        "consensus_threshold"),
                    "scored_images": comp.get("summary", {}).get(
                        "consensus_scored_images"),
                    "passed_images": comp.get("summary", {}).get(
                        "consensus_passed_images"),
                    "failed_images": comp.get("summary", {}).get(
                        "consensus_failed_images"),
                    "pass_rate": comp.get("summary", {}).get(
                        "consensus_pass_rate"),
                    "mean_score": comp.get("summary", {}).get(
                        "consensus_mean_score"),
                    "global_score": comp.get("summary", {}).get(
                        "consensus_global_score"),
                    "imagens_criticas": comp.get("images_low_consensus", []),
                    "imagens_reprovadas": comp.get(
                        "images_failed_consensus", []),
                    "imagens_aprovadas": comp.get(
                        "images_passed_consensus", []),
                },
                "graficos": charts or [],
                "comparacao": comp,
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            return {"ok": True, "path": path}
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}

    def export_charts(self, charts):
        """Salva PNGs vindos do front-end. charts = [{name, dataUrl}]."""
        try:
            res = self.window.create_file_dialog(_fd("folder"))
            if not res:
                return {"ok": False, "cancelled": True}
            folder = res[0]
            saved = []
            for ch in charts or []:
                data_url = ch.get("dataUrl", "")
                if "," not in data_url:
                    continue
                raw = base64.b64decode(data_url.split(",", 1)[1])
                safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", ch.get("name", "grafico"))
                fpath = os.path.join(folder, safe + ".png")
                with open(fpath, "wb") as fh:
                    fh.write(raw)
                saved.append(fpath)
            return {"ok": True, "saved": saved, "folder": folder}
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}

    def export_html_report(self, charts):
        if not self.comparison:
            return {"ok": False, "error": "Não há nada para exportar"}
        try:
            path = self._save_dialog("benaext_relatorio.html",
                                     ("HTML (*.html)",))
            if not path:
                return {"ok": False, "cancelled": True}
            html = build_report_html(self.comparison, self.get_status(),
                                     charts or [])
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
            return {"ok": True, "path": path}
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}

    def export_pdf_report(self, charts=None, anonymize=False):
        if not self.comparison:
            return {"ok": False, "error": "Não há nada para exportar"}
        if not _ensure_pillow():
            return {"ok": False, "error": "Pillow nao esta disponivel para gerar PDF"}
        try:
            path = self._save_dialog("BenaExt Relatorio.pdf",
                                     ("PDF (*.pdf)",))
            if not path:
                return {"ok": False, "cancelled": True}
            comp = _anonymized_comparison(self.comparison) if anonymize else self.comparison
            build_report_pdf(comp, self._export_status(anonymize),
                             charts or [], path)
            return {"ok": True, "path": path}
        except Exception as exc:
            return {"ok": False, "error": _fmt_exc(exc)}


def _fmt_exc(exc):
    return "%s: %s" % (type(exc).__name__, exc)


def _force_process_exit(exit_code=0):
    """Encerra imediatamente; nao chama Qt porque o backend pode travar."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        time.sleep(0.20)  # delay minimo para o compositor se limpar
    except Exception:
        pass
    os._exit(exit_code)


# ---------------------------------------------------------------------------
# Exportacoes (helpers)
# ---------------------------------------------------------------------------
def _build_csv_rows(comp):
    rows = []
    for im in comp["images"]:
        meta = im["meta"]
        orig = "; ".join(sorted(e["nome"] for e in im["original_erros"]))
        consensus = im.get("label_consensus") or {}
        for user, ud in im["users"].items():
            if not ud.get("labeled"):
                rows.append({
                    "arquivo": meta["arquivo"], "usuario": user,
                    "rotulado": "nao", "no_zip": im.get("in_zip"),
                    "original_erros": orig, "usuario_erros": "",
                    "tp": "", "fp": "", "fn": "",
                    "concordancia_total": "", "concordancia_parcial": "",
                    "jaccard": "", "aval_mae": "",
                    "consenso_rotulos": consensus.get("score"),
                    "consenso_passou": consensus.get("passed"),
                    "consenso_ok": consensus.get("correct_votes"),
                    "consenso_extra": consensus.get("extra_votes"),
                    "consenso_faltou": consensus.get("omitted_votes"),
                    "consenso_denominador": consensus.get("denominator"),
                })
                continue
            uerr = "; ".join(sorted(e["nome"] for e in ud.get("erros", [])))
            diffs = ud.get("aval_diffs", [])
            mae = (round(sum(d["diff"] for d in diffs) / len(diffs), 3)
                   if diffs else "")
            rows.append({
                "arquivo": meta["arquivo"], "usuario": user,
                "rotulado": "sim", "no_zip": im.get("in_zip"),
                "original_erros": orig, "usuario_erros": uerr,
                "tp": len(ud.get("tp", [])), "fp": len(ud.get("fp", [])),
                "fn": len(ud.get("fn", [])),
                "concordancia_total": ud.get("exact"),
                "concordancia_parcial": ud.get("partial"),
                "jaccard": ud.get("jaccard"), "aval_mae": mae,
                "consenso_rotulos": consensus.get("score"),
                "consenso_passou": consensus.get("passed"),
                "consenso_ok": consensus.get("correct_votes"),
                "consenso_extra": consensus.get("extra_votes"),
                "consenso_faltou": consensus.get("omitted_votes"),
                "consenso_denominador": consensus.get("denominator"),
            })
    if not rows:
        rows = [{"info": "sem dados"}]
    return rows


def _user_alias_map(comp):
    names = []
    for u in comp.get("users", []):
        name = u.get("usuario")
        if name is not None and name not in names:
            names.append(name)
    for name in comp.get("inter_user", {}).get("users", []):
        if name is not None and name not in names:
            names.append(name)
    for im in comp.get("images", []):
        for name in im.get("users", {}).keys():
            if name is not None and name not in names:
                names.append(name)
    return {name: "Usuario %d" % (i + 1) for i, name in enumerate(names)}


def _replace_user_tokens(text, aliases):
    text = "" if text is None else str(text)
    for original, alias in aliases.items():
        text = text.replace("[%s]" % original, "[%s]" % alias)
    return text


def _anonymized_comparison(comp):
    aliases = _user_alias_map(comp)
    data = copy.deepcopy(comp)
    for u in data.get("users", []):
        u["usuario"] = aliases.get(u.get("usuario"), u.get("usuario"))
    data["ranking"] = [aliases.get(name, name)
                       for name in data.get("ranking", [])]
    inter = data.get("inter_user", {})
    inter["users"] = [aliases.get(name, name)
                      for name in inter.get("users", [])]
    for im in data.get("images", []):
        im["users"] = {aliases.get(name, name): value
                       for name, value in im.get("users", {}).items()}
    return data


def _pdf_font(size=18, bold=False):
    try:
        from PIL import ImageFont
        candidates = (
            r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        )
        for path in candidates:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        return ImageFont.load_default()
    except Exception:
        return None


def _text_w(draw, text, font):
    if not text:
        return 0
    box = draw.textbbox((0, 0), str(text), font=font)
    return box[2] - box[0]


def _text_h(draw, text, font):
    box = draw.textbbox((0, 0), str(text or "Ag"), font=font)
    return box[3] - box[1]


def _wrap_text(draw, text, font, width):
    text = "" if text is None else str(text)
    out = []
    for para in text.splitlines() or [""]:
        words = para.split()
        if not words:
            out.append("")
            continue
        line = words[0]
        for word in words[1:]:
            cand = line + " " + word
            if _text_w(draw, cand, font) <= width:
                line = cand
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


def _chart_image_from_data_url(data_url):
    if not data_url or "," not in data_url:
        return None
    if not _ensure_pillow():
        return None
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


PDF_BASE_DPI = 150
PDF_OUTPUT_DPI = 600


class _PdfReport:
    def __init__(self, title):
        self.dpi = PDF_OUTPUT_DPI
        self.scale = self.dpi / float(PDF_BASE_DPI)
        self.w, self.h = self.px(1240), self.px(1754)
        self.margin = self.px(70)
        self.pages = []
        self.title_font = _pdf_font(self.px(36), True)
        self.h1_font = _pdf_font(self.px(25), True)
        self.h2_font = _pdf_font(self.px(20), True)
        self.text_font = _pdf_font(self.px(17), False)
        self.small_font = _pdf_font(self.px(14), False)
        self.bold_font = _pdf_font(self.px(17), True)
        self.muted = (88, 103, 132)
        self.text = (27, 36, 55)
        self.line = (205, 214, 232)
        self.blue = (37, 99, 235)
        self.new_page()
        self.add_title(title)

    def px(self, value):
        return max(1, int(round(value * self.scale)))

    def new_page(self):
        page = Image.new("RGB", (self.w, self.h), "white")
        from PIL import ImageDraw
        self.draw = ImageDraw.Draw(page)
        self.page = page
        self.pages.append(page)
        self.y = self.margin

    def ensure(self, height):
        if self.y + height > self.h - self.margin:
            self.new_page()

    def add_title(self, text):
        self.draw.text((self.margin, self.y), text, fill=self.blue,
                       font=self.title_font)
        self.y += self.px(52)

    def heading(self, text, level=1):
        font = self.h1_font if level == 1 else self.h2_font
        self.ensure(self.px(54))
        self.y += self.px(16) if self.y > self.margin else 0
        self.draw.text((self.margin, self.y), text, fill=self.blue, font=font)
        self.y += _text_h(self.draw, text, font) + self.px(14)
        self.draw.line((self.margin, self.y, self.w - self.margin, self.y),
                       fill=self.line, width=self.px(2))
        self.y += self.px(14)

    def para(self, text, font=None, fill=None, gap=10):
        font = font or self.text_font
        fill = fill or self.text
        max_w = self.w - self.margin * 2
        lines = _wrap_text(self.draw, text, font, max_w)
        gap_px = self.px(gap)
        line_h = _text_h(self.draw, "Ag", font) + self.px(7)
        self.ensure(line_h * len(lines) + gap_px)
        for line in lines:
            self.draw.text((self.margin, self.y), line, fill=fill, font=font)
            self.y += line_h
        self.y += gap_px

    def table(self, headers, rows, widths=None, max_rows=None):
        if max_rows is not None:
            rows = rows[:max_rows]
        total_w = self.w - self.margin * 2
        if not widths:
            widths = [1] * len(headers)
        scale = total_w / float(sum(widths))
        widths = [int(w * scale) for w in widths]
        widths[-1] += total_w - sum(widths)
        x0 = self.margin
        pad = self.px(8)
        head_h = self.px(34)
        self.ensure(head_h + self.px(8))
        x = x0
        self.draw.rectangle((x0, self.y, x0 + total_w, self.y + head_h),
                            fill=(235, 241, 255), outline=self.line)
        for i, h in enumerate(headers):
            self.draw.text((x + pad, self.y + pad), str(h), fill=self.text,
                           font=self.bold_font)
            x += widths[i]
        self.y += head_h
        for row in rows or [["sem dados"] + [""] * (len(headers) - 1)]:
            cells = list(row)[:len(headers)]
            cells += [""] * (len(headers) - len(cells))
            wrapped = []
            row_h = 0
            for i, cell in enumerate(cells):
                lines = _wrap_text(self.draw, cell, self.small_font,
                                   max(self.px(20), widths[i] - pad * 2))
                wrapped.append(lines)
                row_h = max(row_h, len(lines) * self.px(21) + pad * 2)
            row_h = max(row_h, self.px(34))
            self.ensure(row_h)
            x = x0
            for i, lines in enumerate(wrapped):
                self.draw.rectangle((x, self.y, x + widths[i],
                                     self.y + row_h), outline=self.line)
                yy = self.y + pad
                for line in lines:
                    self.draw.text((x + pad, yy), line, fill=self.text,
                                   font=self.small_font)
                    yy += self.px(21)
                x += widths[i]
            self.y += row_h
        self.y += self.px(16)

    def image(self, img, title=None):
        if img is None:
            return
        if title:
            self.para(title, font=self.bold_font, gap=4)
        max_w = self.w - self.margin * 2
        max_h = self.px(520)
        scale = min(max_w / img.width, max_h / img.height, 1.0)
        out = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))), Image.LANCZOS)
        bottom_gap = self.px(18)
        self.ensure(out.height + bottom_gap)
        x = self.margin + (max_w - out.width) // 2
        self.page.paste(out, (x, self.y))
        self.y += out.height + bottom_gap

    def save(self, path):
        if not self.pages:
            return
        self.pages[0].save(path, "PDF", resolution=self.dpi, save_all=True,
                           append_images=self.pages[1:])


def _pct_str(x, nd=1):
    return "-" if x is None else ("%.*f%%" % (nd, float(x) * 100))


def _join_list(values, limit=8):
    values = list(values or [])
    if not values:
        return "-"
    shown = values[:limit]
    suffix = "" if len(values) <= limit else " (+%d)" % (len(values) - limit)
    return ", ".join(str(x) for x in shown) + suffix


def build_report_pdf(comp, status, charts, path):
    """Gera um relatorio PDF completo usando apenas Pillow."""
    if not _ensure_pillow():
        raise RuntimeError("Pillow nao esta disponivel")
    s = comp.get("summary", {})
    pdf = _PdfReport("BenaExt - Relatorio completo")
    pdf.para("Gerado em %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             fill=pdf.muted)
    pdf.para("Exportação com metricas, graficos, tabelas de erro, matriz de "
             "concordancia e detalhamento por imagem.")

    pdf.heading("Resumo")
    pdf.table(["Item", "Valor"], [
        ["Imagens", s.get("total_images", 0)],
        ["Imagens com gabarito", s.get("total_images_with_reference", 0)],
        ["Usuarios", s.get("total_users", 0)],
        ["Erros no gabarito", s.get("total_original_errors", 0)],
        ["Imagens divergentes", s.get("total_divergent_images", 0)],
        ["Concordancia total", s.get("total_full_agreement_images", 0)],
        ["Tipos de erro", s.get("error_types", 0)],
        ["Consenso rotulos - limite", _pct_str(s.get("consensus_threshold"))],
        ["Consenso rotulos - avaliadas",
         s.get("consensus_scored_images", 0)],
        ["Consenso rotulos - passou",
         "%s/%s" % (s.get("consensus_passed_images", 0),
                    s.get("consensus_scored_images", 0))],
        ["Consenso rotulos - nao passou",
         "%s/%s" % (s.get("consensus_failed_images", 0),
                    s.get("consensus_scored_images", 0))],
        ["Consenso rotulos - media",
         _pct_str(s.get("consensus_mean_score"))],
        ["Consenso rotulos - global",
         _pct_str(s.get("consensus_global_score"))],
    ], [2, 1])

    pdf.heading("Consenso por rotulos")
    pdf.para("Pontuacao por imagem: OK / (OK + Extra + Faltou). "
             "A imagem passa quando a pontuacao atinge o limite do resumo.")
    consensus_rows = []
    for x in comp.get("images_low_consensus", []):
        consensus_rows.append([
            x.get("arquivo"),
            _pct_str(x.get("score")),
            "sim" if x.get("passed") else "nao",
            "%s/%s" % (x.get("correct_votes"), x.get("denominator")),
            x.get("extra_votes"),
            x.get("omitted_votes"),
            x.get("comparable_users"),
        ])
    pdf.table(["Arquivo", "Consenso", "Passou", "OK/Total", "Extra",
               "Faltou", "Usuarios"], consensus_rows,
              [3.3, .9, .8, .9, .8, .8, .8])

    pdf.heading("Metricas por usuario")
    user_rows = []
    for u in comp.get("users", []):
        user_rows.append([
            u.get("usuario"),
            u.get("labeled_images"),
            u.get("user_errors_total"),
            _pct_str(u.get("precision")),
            _pct_str(u.get("recall")),
            _pct_str(u.get("f1")),
            _pct_str(u.get("jaccard")),
            _pct_str(u.get("exact_rate")),
            u.get("fp"),
            u.get("fn"),
            "-" if u.get("aval_mae") is None else "%.2f" % u.get("aval_mae"),
        ])
    pdf.table(["Usuario", "Imagens", "Marcou", "P", "R", "F1", "Jaccard",
               "Conc.", "FP", "FN", "MAE"], user_rows,
              [2.2, .8, .9, .8, .8, .8, .9, .9, .7, .7, .8])

    pdf.heading("Graficos")
    added_chart = False
    for ch in charts or []:
        img = _chart_image_from_data_url(ch.get("dataUrl"))
        if img is None:
            continue
        added_chart = True
        pdf.image(img, ch.get("title") or ch.get("name") or "Grafico")
    if not added_chart:
        pdf.para("Sem graficos disponiveis no momento da exportação.",
                 fill=pdf.muted)

    pdf.heading("Erros por tipo")
    err_rows = []
    for e in comp.get("errors", []):
        err_rows.append([e.get("nome"), e.get("orig_count"),
                         e.get("user_count"), e.get("tp"), e.get("fp"),
                         e.get("fn"), _pct_str(e.get("agreement"))])
    pdf.table(["Erro", "Gabarito", "Usuarios", "TP", "FP", "FN", "Conc."],
              err_rows, [3.8, 1, 1, .8, .8, .8, 1])

    pdf.heading("Matriz de confusao")
    conf = comp.get("confusion", {})
    labels = conf.get("labels", [])
    matrix = conf.get("matrix", [])
    conf_rows = []
    for i, row in enumerate(matrix):
        for j, val in enumerate(row):
            if val:
                conf_rows.append([labels[i], labels[j], val])
    pdf.table(["Gabarito", "Marcado pelo usuario", "Qtd"], conf_rows,
              [2.5, 2.5, .8])

    pdf.heading("Concordancia entre usuarios")
    inter = comp.get("inter_user", {})
    users = inter.get("users", [])
    pair_rows = []
    for i, row in enumerate(inter.get("matrix", [])):
        for j, val in enumerate(row):
            if i < j:
                pair_rows.append([users[i], users[j], _pct_str(val, 0)])
    pdf.table(["Usuario A", "Usuario B", "Jaccard medio"], pair_rows,
              [2, 2, 1])

    pdf.heading("Imagens com maior divergencia")
    div_rows = [[x.get("arquivo"), _pct_str(x.get("inter_user_agreement")),
                 x.get("comparable_users")]
                for x in comp.get("images_most_divergent", [])]
    pdf.table(["Arquivo", "Concordancia", "Usuarios"], div_rows,
              [4, 1, 1])

    pdf.heading("Detalhe por imagem")
    for im in comp.get("images", []):
        meta = im.get("meta", {})
        lc = im.get("label_consensus") or {}
        pdf.heading(str(meta.get("arquivo") or im.get("base")), level=2)
        pdf.table(["Campo", "Valor"], [
            ["Data", meta.get("data") or "-"],
            ["ID", meta.get("id") or "-"],
            ["Dedo", meta.get("dedo") or "-"],
            ["Grupo", meta.get("grupo") or "-"],
            ["Tem gabarito", "sim" if im.get("has_reference") else "nao"],
            ["Usuarios comparaveis", im.get("comparable_users")],
            ["Concordancia entre usuarios", _pct_str(im.get("inter_user_agreement"))],
            ["Consenso por rotulos", _pct_str(lc.get("score"))],
            ["Passou no consenso",
             "-" if lc.get("passed") is None
             else ("sim" if lc.get("passed") else "nao")],
            ["Consenso OK/Total",
             ("%s/%s" % (lc.get("correct_votes"), lc.get("denominator"))
              if lc else "-")],
            ["Consenso extra/faltou",
             ("%s/%s" % (lc.get("extra_votes"), lc.get("omitted_votes"))
              if lc else "-")],
        ], [1, 3])
        pdf.para("Gabarito: %s" %
                 _join_list([e.get("nome") for e in im.get("original_erros", [])],
                            limit=20),
                 font=pdf.small_font)
        if lc.get("details"):
            bits = []
            for d in lc.get("details", []):
                label_state = "ok" if d.get("in_original") else "extra"
                miss = d.get("missing_votes", 0)
                tail = "" if not miss else ", faltou %s" % miss
                bits.append("%s: %s/%s %s%s" % (
                    d.get("nome"), d.get("votes"),
                    lc.get("comparable_users"), label_state, tail))
            pdf.para("Consenso rotulos: %s" %
                     _join_list(bits, limit=18), font=pdf.small_font)
        rows = []
        for name, ud in im.get("users", {}).items():
            if not ud.get("labeled"):
                rows.append([name, "nao rotulado", "-", "-", "-", "-"])
                continue
            if ud.get("no_reference"):
                rows.append([name, "sem gabarito", _join_list(
                    [e.get("nome") for e in ud.get("erros", [])], 12),
                    "-", "-", "-"])
                continue
            rows.append([
                name,
                "total" if ud.get("exact") else ("parcial" if ud.get("partial") else "divergente"),
                _join_list([e.get("nome") for e in ud.get("erros", [])], 10),
                _join_list(ud.get("fp"), 8),
                _join_list(ud.get("fn"), 8),
                _pct_str(ud.get("jaccard"), 0),
            ])
        pdf.table(["Usuario", "Status", "Marcados", "Extra", "Faltou", "Jacc."],
                  rows, [1.2, 1, 2.2, 1.4, 1.4, .8])

    warnings = (status or {}).get("warnings") or []
    if warnings:
        pdf.heading("Avisos")
        pdf.table(["Aviso"], [[w] for w in warnings], [1])

    pdf.save(path)


def build_report_html(comp, status, charts):
    """Relatorio HTML estatico e autocontido."""
    s = comp["summary"]
    def esc(x):
        return ("" if x is None else str(x)).replace("&", "&amp;") \
            .replace("<", "&lt;").replace(">", "&gt;")

    user_rows = "".join(
        "<tr><td>%s</td><td>%d</td><td>%.1f%%</td><td>%.1f%%</td>"
        "<td>%.1f%%</td><td>%.1f%%</td><td>%.1f%%</td><td>%d</td>"
        "<td>%d</td><td>%s</td></tr>" % (
            esc(u["usuario"]), u["labeled_images"], u["precision"] * 100,
            u["recall"] * 100, u["f1"] * 100, u["jaccard"] * 100,
            u["exact_rate"] * 100, u["fp"], u["fn"],
            ("%.2f" % u["aval_mae"]) if u["aval_mae"] is not None else "-")
        for u in comp["users"])

    consensus_rows = "".join(
        "<tr><td>%s</td><td>%.1f%%</td><td>%s</td><td>%s/%s</td>"
        "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(x.get("arquivo")), float(x.get("score") or 0) * 100,
            "sim" if x.get("passed") else "nao",
            x.get("correct_votes"), x.get("denominator"),
            x.get("extra_votes"), x.get("omitted_votes"),
            x.get("comparable_users"))
        for x in comp.get("images_low_consensus", []))
    if not consensus_rows:
        consensus_rows = '<tr><td colspan="7"><small>sem dados</small></td></tr>'

    chart_imgs = "".join(
        '<div class="chart"><h3>%s</h3><img src="%s"></div>' %
        (esc(c.get("title", c.get("name", ""))), c.get("dataUrl", ""))
        for c in charts if c.get("dataUrl"))

    omit_rows = "".join("<tr><td>%s</td><td>%d</td></tr>" %
                        (esc(e["nome"]), e["fn"]) for e in comp["most_omitted"])
    extra_rows = "".join("<tr><td>%s</td><td>%d</td></tr>" %
                         (esc(e["nome"]), e["fp"]) for e in comp["most_extra"])

    # CSS mantido fora do bloco formatado (contem '%' literais como width:100%)
    head = (
        '<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">'
        '<title>BenaExt - Relatorio</title><style>'
        'body{font-family:Segoe UI,Arial,sans-serif;background:#0f1420;'
        'color:#e6ebf5;margin:0;padding:32px}'
        'h1{color:#6ea8fe}h2{color:#8bb4ff;border-bottom:1px solid #2a3550;'
        'padding-bottom:6px;margin-top:34px}'
        'table{border-collapse:collapse;width:100%;margin:12px 0}'
        'th,td{border:1px solid #2a3550;padding:8px 10px;text-align:left;'
        'font-size:14px}th{background:#1a2236}tr:nth-child(even){background:#141b2b}'
        '.cards{display:flex;gap:14px;flex-wrap:wrap}.card{background:#1a2236;'
        'border:1px solid #2a3550;border-radius:10px;padding:16px 20px;min-width:150px}'
        '.card b{display:block;font-size:26px;color:#6ea8fe}.chart{margin:16px 0}'
        '.chart img{max-width:100%;background:#141b2b;border:1px solid #2a3550;'
        'border-radius:8px}small{color:#90a0c0}</style></head><body>')

    body_tpl = """
<h1>BenaExt</h1>
<small>Gerado em %s</small>
<h2>Resumo</h2><div class="cards">
<div class="card"><b>%d</b>Imagens</div>
<div class="card"><b>%d</b>Usuarios</div>
<div class="card"><b>%d</b>Erros no gabarito</div>
<div class="card"><b>%d</b>Imagens divergentes</div>
<div class="card"><b>%d</b>Concordancia total</div>
<div class="card"><b>%d</b>Tipos de erro</div>
<div class="card"><b>%s</b>Consenso medio</div>
<div class="card"><b>%d/%d</b>Passou consenso</div>
<div class="card"><b>%d/%d</b>Nao passou consenso</div>
</div>
<h2>Consenso por rotulos</h2>
<table><tr><th>Arquivo</th><th>Consenso</th><th>Passou</th><th>OK/Total</th>
<th>Extra</th><th>Faltou</th><th>Usuarios</th></tr>{consensus_rows}</table>
<h2>Metricas por usuario</h2>
<table><tr><th>Usuario</th><th>Imagens</th><th>Precisao</th><th>Recall</th>
<th>F1</th><th>Jaccard</th><th>Conc. total</th><th>FP</th><th>FN</th>
<th>MAE aval.</th></tr>{user_rows}</table>
<h2>Graficos</h2>{chart_imgs}
<h2>Erros mais omitidos (FN)</h2><table><tr><th>Erro</th><th>FN</th></tr>{omit_rows}</table>
<h2>Erros mais marcados indevidamente (FP)</h2><table><tr><th>Erro</th><th>FP</th></tr>{extra_rows}</table>
</body></html>"""
    # 1) formata os numeros do resumo; 2) insere as tabelas (que contem '%')
    body = (body_tpl % (
        esc(s.get("generated_at")), s["total_images"], s["total_users"],
        s["total_original_errors"], s["total_divergent_images"],
        s["total_full_agreement_images"], s["error_types"],
        _pct_str(s.get("consensus_mean_score")),
        s.get("consensus_passed_images", 0),
        s.get("consensus_scored_images", 0),
        s.get("consensus_failed_images", 0),
        s.get("consensus_scored_images", 0))) \
        .replace("{consensus_rows}", consensus_rows) \
        .replace("{user_rows}", user_rows) \
        .replace("{chart_imgs}", chart_imgs or "<small>(sem graficos)</small>") \
        .replace("{omit_rows}", omit_rows) \
        .replace("{extra_rows}", extra_rows)

    return head + body


# ---------------------------------------------------------------------------
# Interface (HTML/CSS/JS) -- preenchida na proxima etapa
# ---------------------------------------------------------------------------
def build_index_html():
    return INDEX_HTML


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BenaExt</title>
<style>
:root{
  --bg:#0f1420; --panel:#161d2e; --panel2:#1a2236; --panel3:#10172a;
  --border:#283250; --border2:#324066;
  --text:#e6ebf5; --muted:#90a0c0; --muted2:#6b7a9c;
  --accent:#6ea8fe; --accent2:#8bb4ff;
  --good:#3ddc97; --bad:#ff7b72; --warn:#ffb454; --div:#d2a8ff;
  --shadow:0 6px 24px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
html,body{height:100%;margin:0;overflow:hidden}
body{
  font-family:"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:var(--bg); color:var(--text); font-size:15px;
  -webkit-user-select:none; user-select:none;
}
button{font-family:inherit;color:inherit;cursor:pointer}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#2c3856;border-radius:6px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:#3a4a72}

#app{display:flex;flex-direction:column;height:100vh}

/* ---- Titlebar ---- */
.titlebar{
  height:46px;flex:0 0 46px;display:flex;align-items:center;
  background:linear-gradient(180deg,#141b2b,#10162400);
  background-color:#131a2a;border-bottom:1px solid var(--border);
}
.tb-left{display:flex;align-items:baseline;gap:10px;padding:0 16px;height:100%;
  display:flex;align-items:center}
.brand{font-weight:700;font-size:16px;letter-spacing:.4px;
  background:linear-gradient(90deg,#6ea8fe,#8bb4ff,#d2a8ff);
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.tb-sub{color:var(--muted2);font-size:12px}
.tb-mid{flex:1;height:100%}
.winbtns{display:flex;height:100%}
.winbtn{width:48px;height:46px;border:0;background:transparent;
  color:var(--muted);display:flex;align-items:center;justify-content:center}
.winbtn svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
.winbtn:hover{background:#1e2740;color:var(--text)}
.winbtn.refresh:hover{color:var(--accent)}
.winbtn.close:hover{background:#c0392b;color:#fff}

/* ---- Toolbar ---- */
.toolbar{display:flex;align-items:center;gap:10px;padding:10px 16px;
  background:var(--panel3);border-bottom:1px solid var(--border);flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 15px;border-radius:9px;
  border:1px solid var(--border2);background:#1c2740;color:var(--text);font-size:14px;
  font-weight:600;transition:.15s;white-space:nowrap}
.btn:hover{background:#243257;border-color:#3a4d7d}
.btn.primary{background:linear-gradient(180deg,#2c5bd6,#2049b8);border-color:#3a63d6}
.btn.primary:hover{background:linear-gradient(180deg,#3568e8,#2851c8)}
.ic{display:inline-flex;align-items:center;justify-content:center}
.ic svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
.btn.sm{padding:8px 10px;font-size:13px;border-radius:7px}
.btn.sm .ic svg{width:15px;height:15px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:20px;
  background:#161f33;border:1px solid var(--border);font-size:13px;color:var(--muted)}
.chip .dot{width:8px;height:8px;border-radius:50%;background:#5b6580}
.chip.on{color:var(--text);border-color:var(--good)}
.chip.on .dot{background:var(--good)}
.spacer{flex:1}
.anon-toggle{position:relative;display:inline-flex;align-items:center;gap:8px;height:40px;
  padding:0 12px;border:1px solid var(--border2);border-radius:9px;background:#1c2740;
  color:var(--muted);font-size:13px;font-weight:700;white-space:nowrap;cursor:pointer}
.anon-toggle:hover{background:#243257;border-color:#3a4d7d;color:var(--text)}
.anon-toggle input{position:absolute;opacity:0;pointer-events:none}
.anon-toggle .check-mark{width:14px;height:14px;border-radius:4px;border:1px solid #46577d;
  background:#141c30;display:inline-flex;align-items:center;justify-content:center;flex:0 0 14px}
.anon-toggle input:checked + .check-mark{border-color:var(--accent);background:var(--accent)}
.anon-toggle input:checked + .check-mark:after{content:"";width:7px;height:4px;border-left:2px solid #06122e;
  border-bottom:2px solid #06122e;transform:rotate(-45deg);margin-top:-1px}

/* ---- Tabs ---- */
.tabs{display:flex;gap:4px;padding:0 16px;background:var(--panel3);
  border-bottom:1px solid var(--border)}
.tab{padding:12px 18px;border:0;background:transparent;color:var(--muted);font-weight:600;
  font-size:14px;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent2);border-bottom-color:var(--accent)}

#main{flex:1;overflow:hidden;position:relative}

.view{
  position:absolute;
  inset:0;
  overflow:hidden;
  display:none;
}

.view.active{
  display:block;
}

#view-compare.active{
  display:flex;
  flex-direction:column;
  min-height:0;
}

/* ---- Empty state ---- */
#empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:16px;color:var(--muted);text-align:center;z-index:5;
  background:var(--bg)}
#empty .ebadge{width:74px;height:74px;border-radius:20px;display:flex;align-items:center;
  justify-content:center;background:#16203a;border:1px solid var(--border2)}
#empty .ebadge svg{width:34px;height:34px;stroke:var(--accent);fill:none;stroke-width:1.7;
  stroke-linecap:round;stroke-linejoin:round}
#empty h2{color:var(--text);margin:0;font-size:22px}
#empty .hint{max-width:540px;line-height:1.7}
#empty.hidden{display:none}

/* ---- Compare layout ---- */
.filters{
  display:flex;
  gap:14px;
  align-items:flex-end;
  padding:12px 16px;
  flex-wrap:wrap;
  border-bottom:1px solid var(--border);
  background:var(--panel3);
  flex:0 0 auto;
}

.field{display:flex;flex-direction:column;gap:5px}
.field > label{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted2);
  height:13px;line-height:13px}
input[type=text],select{background:#10172a;border:1px solid var(--border2);color:var(--text);
  height:38px;padding:0 11px;border-radius:8px;font-size:14px;outline:none;min-width:150px}
#selError{width:230px}
input[type=text]:focus,select:focus{border-color:var(--accent)}
.checkrow{display:flex;gap:8px}
.check{position:relative;display:inline-flex;align-items:center;gap:8px;height:36px;
  padding:0 12px;border:1px solid var(--border);border-radius:8px;background:#10172a;
  color:var(--muted);font-size:13px;font-weight:600;cursor:pointer;transition:.15s;
  white-space:nowrap}
.check:hover{border-color:#3a4d7d;color:var(--text);background:#151f36}
.check:has(input:checked){border-color:var(--accent);background:rgba(110,168,254,.14);color:var(--text)}
.check input{position:absolute;opacity:0;pointer-events:none}
.check-mark{width:14px;height:14px;border-radius:4px;border:1px solid #46577d;background:#141c30;
  display:inline-flex;align-items:center;justify-content:center;transition:.15s;flex:0 0 14px}
.check input:checked + .check-mark{border-color:var(--accent);background:var(--accent)}
.check input:checked + .check-mark:after{content:"";width:7px;height:4px;border-left:2px solid #06122e;
  border-bottom:2px solid #06122e;transform:rotate(-45deg);margin-top:-1px}
.filters .info-field{margin-left:auto;align-self:flex-end;padding-bottom:9px}

.compare-grid{
  flex:1 1 auto;
  min-height:0;
  display:grid;
  grid-template-columns:minmax(420px,1.05fr) minmax(380px,1fr);
  gap:14px;
  padding:14px 16px 24px 16px;
  height:auto;
  overflow:hidden;
}

body.easter-video-active .compare-grid{
  grid-template-columns:1fr;
}

body.easter-video-active .side{
  display:none;
}

body.easter-celebration-active .compare-grid{
  grid-template-columns:1fr;
}

body.easter-celebration-active .side{
  display:none;
}

.viewer{
  display:flex;
  flex-direction:column;
  min-height:0;
  background:var(--panel);
  border:1px solid var(--border);
  border-radius:14px;
  overflow:hidden;
}

.modes{display:flex;gap:6px;padding:10px;border-bottom:1px solid var(--border);flex-wrap:wrap;
  background:var(--panel2)}
.modebtn{padding:7px 12px;border-radius:8px;border:1px solid var(--border2);background:#1a2338;
  font-size:13px;font-weight:600;color:var(--muted)}
.modebtn:hover{color:var(--text);border-color:#3a4d7d}
.modebtn.active{background:var(--accent);border-color:var(--accent);color:#06122e}
.zoom-tools{margin-left:auto;display:flex;align-items:center;gap:4px}
.zoom-btn{width:34px;height:34px;border-radius:8px;border:1px solid var(--border2);
  background:#1a2338;color:var(--muted);display:flex;align-items:center;justify-content:center}
.zoom-btn svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
.zoom-btn:hover{color:var(--accent);border-color:var(--accent)}
.zoom-btn:disabled{opacity:.35;cursor:default;color:var(--muted2);border-color:var(--border)}
.zoom-field{display:flex;align-items:center;gap:2px;padding:0}
input.zoom-input[type=text]{width:46px;min-width:46px;max-width:46px;height:26px;border-radius:6px;
  border:1px solid var(--border2);background:#10172a;color:var(--muted);text-align:center;
  font-size:14px;padding:0;outline:none;font-variant-numeric:tabular-nums;flex:0 0 auto}
input.zoom-input[type=text]:focus{border-color:var(--accent);color:var(--text)}

.img-stage{
  flex:1;
  min-height:0;
  position:relative;
  overflow:auto;
  background:#0c1220;
  scrollbar-gutter:stable both-edges;
}

body.easter-celebration-active .img-stage,
body.easter-video-active .img-stage{
  overflow:hidden;
}

.img-stage.zoomed{cursor:grab}
.img-stage.panning{cursor:grabbing}

.img-stage::-webkit-scrollbar{
  width:11px;
  height:11px;
}

.img-stage::-webkit-scrollbar-track{
  background:#0c1220;
  border-radius:8px;
}

.img-stage::-webkit-scrollbar-thumb{
  background:#2c3856;
  border-radius:8px;
  border:2px solid #0c1220;
}

.img-stage::-webkit-scrollbar-thumb:hover{
  background:#3a4a72;
}

.img-stage::-webkit-scrollbar-corner{
  background:#0c1220;
}

#imgScroll{
  width:100%;
  height:100%;
  min-width:100%;
  min-height:100%;
  display:flex;
  align-items:center;
  justify-content:center;
  padding:14px;
  box-sizing:border-box;
}

#imgWrap{
  position:relative;
  display:inline-flex;
  flex:0 0 auto;
  border-radius:6px;
  overflow:hidden;
}

#imgWrap.checker{background-image:
  linear-gradient(45deg,#26314d 25%,transparent 25%),
  linear-gradient(-45deg,#26314d 25%,transparent 25%),
  linear-gradient(45deg,transparent 75%,#26314d 75%),
  linear-gradient(-45deg,transparent 75%,#26314d 75%);
  background-size:22px 22px;background-position:0 0,0 11px,11px -11px,-11px 0;background-color:#1a2338}
#imgEl{display:block;max-width:none;max-height:none;object-fit:contain}
#easterVideo{display:none;position:absolute;inset:0;width:100%;height:100%;
  object-fit:contain;background:#050b18}
#easterText{display:none;position:absolute;inset:0;flex-direction:column;
  align-items:center;justify-content:space-between;padding:clamp(4px,1vh,28px) 2vw;
  text-align:center;pointer-events:none;z-index:10}
.easter-word{display:block;max-width:100%;white-space:nowrap;font-weight:900;
  font-size:clamp(52px,10vw,190px);line-height:.82;letter-spacing:0;
  text-transform:uppercase;text-shadow:0 5px 0 #003a83,
    0 0 18px rgba(255,255,255,.9),0 0 34px rgba(0,126,255,.9);
  animation:gremioFlash .72s steps(1,end) infinite}
#imgWrap.easter-video{background:#050b18;box-shadow:0 0 32px rgba(0,98,204,.45)}
#imgWrap.easter-video #imgEl{opacity:0}
#imgWrap.easter-video #easterVideo{display:block}
#imgWrap.easter-celebration{background:#050b18;box-shadow:0 0 32px rgba(0,98,204,.45)}
#imgWrap.easter-celebration #imgEl{opacity:1}
body.easter-video-active #easterText,
body.easter-celebration-active #easterText{display:flex}
@keyframes gremioFlash{
  0%,49%{color:#fff}
  50%,100%{color:#0079ff}
}
#imgWrap.noimg{min-width:280px;min-height:280px;align-items:center;justify-content:center;
  border:1px dashed var(--border2)}
.nav{display:flex;align-items:center;gap:10px;padding:10px;border-top:1px solid var(--border);
  background:var(--panel2)}
.navbtn{width:38px;height:38px;border-radius:9px;border:1px solid var(--border2);background:#1a2338;
  display:flex;align-items:center;justify-content:center}
.navbtn svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
.navbtn:hover{border-color:var(--accent);color:var(--accent)}
.navbtn:disabled{opacity:.35;cursor:default}
#counter{font-variant-numeric:tabular-nums;color:var(--muted);font-size:14px}
#imgName{font-size:13px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#imgInfo{font-size:12px;color:var(--muted2)}

.side{
  display:flex;
  flex-direction:column;
  gap:12px;
  min-height:0;
  overflow-y:auto;
  padding-right:4px;
}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:14px}
.card h3{margin:0 0 12px;font-size:14px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent2)}
.meta-grid{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:14px}
.meta-grid .k{color:var(--muted2)}
.badges{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.badge{font-size:12px;padding:4px 10px;border-radius:20px;background:#1c2740;border:1px solid var(--border);color:var(--muted)}
.badge.good{background:rgba(61,220,151,.13);border-color:var(--good);color:var(--good)}
.badge.bad{background:rgba(255,123,114,.13);border-color:var(--bad);color:var(--bad)}
.badge.warn{background:rgba(255,180,84,.13);border-color:var(--warn);color:var(--warn)}
.badge.div{background:rgba(210,168,255,.13);border-color:var(--div);color:var(--div)}
.chips{display:flex;gap:7px;flex-wrap:wrap}
.chipx{font-size:13px;padding:6px 11px;border-radius:8px;border:1px solid var(--border2);
  background:#1a2338;display:inline-flex;align-items:center;gap:6px}
.chipx.tp{border-color:var(--good);background:rgba(61,220,151,.12);color:#bdf5dc}
.chipx.fp{border-color:var(--bad);background:rgba(255,123,114,.12);color:#ffd2cf}
.chipx.fn{border-color:var(--warn);background:rgba(255,180,84,.10);color:#ffe2bb}
.chipx.neutral{border-color:var(--border2);color:var(--text)}
.chipx .av{font-size:11px;opacity:.8;background:#0e1526;border-radius:5px;padding:1px 6px}
.ucard{background:var(--panel2);border:1px solid var(--border);border-radius:11px;padding:11px;margin-bottom:9px}
.ucard.nolabel{opacity:.55}
.ucard-h{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.ucard-h b{font-size:14px}
.avaldiff{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.adiff{font-size:12px;color:var(--muted);background:#10172a;border:1px solid var(--border);
  border-radius:6px;padding:3px 8px}
.adiff.on{color:var(--warn);border-color:var(--warn)}
.legend{display:flex;gap:12px 16px;flex-wrap:wrap;align-items:center;
  font-size:13px;line-height:1.35;color:var(--text)}
.legend i{width:13px;height:13px;border-radius:3px;display:inline-block;margin-right:6px;vertical-align:-2px}

/* ---- Stats ---- */
.stats-scroll{position:absolute;inset:0;overflow-y:auto;padding:16px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.statcard{background:var(--panel);border:1px solid var(--border);border-radius:14px;
  padding:14px 18px;min-width:140px;flex:1}
.statcard .v{font-size:28px;font-weight:700;color:var(--accent2)}
.statcard .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
.section{margin:18px 0}
.section h2{font-size:14px;color:var(--accent2);margin:0 0 10px;
  border-bottom:1px solid var(--border);padding-bottom:8px;display:flex;align-items:center;gap:8px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
.chart-card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:14px}
.chart-card h4{margin:0 0 10px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.chart-card svg{display:block}
table.tbl{width:100%;border-collapse:collapse;font-size:13px}
table.tbl th,table.tbl td{padding:8px 10px;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}
table.tbl th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px;
  position:sticky;top:0;background:var(--panel);cursor:pointer}
table.tbl th:hover{color:var(--text)}
table.tbl td.num,table.tbl th.num{text-align:right;font-variant-numeric:tabular-nums}
table.tbl tr:hover td{background:#19223a}
.copy-img-name{appearance:none;border:0;background:transparent;color:var(--accent2);
  font:inherit;padding:0;text-align:left;cursor:pointer;white-space:nowrap}
.copy-img-name:hover{text-decoration:underline;color:var(--accent)}
.tbl-wrap{background:var(--panel);border:1px solid var(--border);border-radius:14px;overflow:auto;max-height:340px}
.heat{border-collapse:collapse;font-size:11px}
.heat th,.heat td{border:1px solid var(--border);padding:5px 7px;text-align:center;min-width:40px}
.heat th{color:var(--muted);position:sticky;top:0;background:var(--panel)}
.heat td.lbl{text-align:left;color:var(--muted);position:sticky;left:0;background:var(--panel);white-space:nowrap}
.bar-pos{color:var(--bad)} .bar-neg{color:var(--warn)}
.exports{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}

/* ---- Toast / busy ---- */
#toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(40px);
  background:#1c2740;border:1px solid var(--border2);padding:11px 18px;border-radius:10px;
  box-shadow:var(--shadow);opacity:0;transition:.25s;z-index:50;font-size:13px;max-width:70vw}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.good{border-color:var(--good)} #toast.bad{border-color:var(--bad)}
#busy{position:fixed;inset:0;background:rgba(8,12,20,.45);display:none;align-items:center;
  justify-content:center;z-index:40}
#busy.show{display:flex}
.spinner{width:42px;height:42px;border:4px solid #2a3550;border-top-color:var(--accent);
  border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.muted{color:var(--muted)}
.small{font-size:12px}
</style>
</head>
<body>
<div id="app">
  <!-- Titlebar -->
  <div class="titlebar">
    <div class="tb-left">
      <span class="brand">BenaExt</span>
    </div>
    <div class="tb-mid"></div>
    <div class="winbtns">
      <button id="btnMin" class="winbtn" title="Minimizar"><svg viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/></svg></button>
      <button id="btnRefresh" class="winbtn refresh" title="Atualizar (recarrega dados e tela)"><svg viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg></button>
      <button id="btnClose" class="winbtn close" title="Fechar"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
  </div>

  <!-- Toolbar -->
  <div class="toolbar">
    <button id="btnZip" class="btn primary"><span class="ic"><svg viewBox="0 0 24 24"><polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg></span> ZIP de imagens</button>
    <button id="btnOrig" class="btn"><span class="ic"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></span> JSON original</button>
    <button id="btnUsers" class="btn"><span class="ic"><svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></span> Pasta de usuários</button>
    <button id="btnSession" class="btn" title="Recarregar os ultimos arquivos usados"><span class="ic"><svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7"/><polyline points="3 4 3 10 9 10"/><path d="M12 7v5l3 2"/></svg></span> Sessao anterior</button>
    <label class="anon-toggle" title="Exibir e exportar como Usuario 1, Usuario 2...">
      <input type="checkbox" id="anonUsers"><span class="check-mark"></span><span>Anonimizar usuarios</span>
    </label>
    <span class="spacer"></span>
    <span id="chipOrig" class="chip"><span class="dot"></span>original</span>
    <span id="chipUsers" class="chip"><span class="dot"></span>0 usuários</span>
    <span id="chipZip" class="chip"><span class="dot"></span>zip</span>
    <button id="btnWarn" class="btn sm" title="Ver avisos/diagnóstico"><span class="ic"><svg viewBox="0 0 24 24"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span> <span id="warnCount">0</span></button>
  </div>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab active" data-tab="compare">Comparação por imagem</button>
    <button class="tab" data-tab="stats">Estatísticas gerais</button>
  </div>

  <div id="main">
    <!-- COMPARE -->
    <div id="view-compare" class="view active">
      <div class="filters">
        <div class="field"><label>Buscar arquivo</label>
          <input type="text" id="search" placeholder="nome do arquivo..."></div>
        <div class="field"><label>Usuário em foco</label>
          <select id="selUser"><option value="__all__">Todos</option></select></div>
        <div class="field"><label>Tipo de erro</label>
          <select id="selError"><option value="__all__">Todos</option></select></div>
        <div class="field"><label>Filtros</label>
          <div class="checkrow">
            <label class="check"><input type="checkbox" id="fDiv"><span class="check-mark"></span><span>Só divergência</span></label>
            <label class="check"><input type="checkbox" id="fAgree"><span class="check-mark"></span><span>Só concordância total</span></label>
          </div>
        </div>
        <span id="filterInfo" class="small muted info-field"></span>
      </div>
      <div class="compare-grid">
        <div class="viewer">
          <div class="modes" id="modeBar">
            <button class="modebtn active" data-mode="alpha">Calibrada</button>
            <button class="modebtn" data-mode="r">Segmentada</button>
            <div class="zoom-tools">
              <button id="zoomOut" class="zoom-btn" title="Diminuir zoom"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="8" y1="11" x2="14" y2="11"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></button>
              <div class="zoom-field">
                <input id="zoomPct" class="zoom-input" type="text" inputmode="numeric" maxlength="4" value="100%" title="Zoom (%)">
              </div>
              <button id="zoomIn" class="zoom-btn" title="Aumentar zoom"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="8" y1="11" x2="14" y2="11"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></button>
            </div>
          </div>
            <div class="img-stage" id="imgStage">
              <div id="imgScroll">
              <div id="imgWrap"><img id="imgEl" alt=""><video id="easterVideo" playsinline loop preload="auto"></video></div>
            </div>
            <div id="easterText" aria-hidden="true"><span id="easterTextTop" class="easter-word"></span><span id="easterTextBottom" class="easter-word"></span></div>
          </div>
          <div class="nav">
            <button id="btnPrev" class="navbtn" title="Anterior (←)"><svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg></button>
            <button id="btnNext" class="navbtn" title="Próxima (→)"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></button>
            <span id="counter">0 / 0</span>
            <div style="flex:1;min-width:0">
              <div id="imgName">—</div><div id="imgInfo"></div>
            </div>
          </div>
        </div>
        <div class="side">
          <div class="card">
            <h3>Metadados da imagem</h3>
            <div class="meta-grid" id="metaPanel"><span class="muted">—</span></div>
          </div>
          <div class="card">
            <h3>Rotulagem original (gabarito)</h3>
            <div id="origPanel" class="chips"><span class="muted">—</span></div>
          </div>
          <div class="card">
            <h3>Rotulagem dos usuários</h3>
            <div class="legend" style="margin-bottom:10px">
              <span><i style="background:var(--good)"></i>Acerto</span>
              <span><i style="background:var(--bad)"></i>Erro extra (FP)</span>
              <span><i style="background:var(--warn)"></i>Omissão (FN)</span>
              <span><i style="background:var(--div)"></i>Divergência</span>
            </div>
            <div id="usersPanel"><span class="muted">—</span></div>
          </div>
        </div>
      </div>
    </div>

    <!-- STATS -->
    <div id="view-stats" class="view">
      <div class="stats-scroll">
        <div class="cards" id="cards"></div>

        <div class="section">
          <h2>Consenso por rótulos <span id="consensusAvg" class="badge"></span></h2>
          <div class="tbl-wrap"><table class="tbl" id="tblConsensus"></table></div>
        </div>

        <div class="section">
          <h2>Métricas por usuário</h2>
          <div class="tbl-wrap"><table class="tbl" id="userTable"></table></div>
          <div class="small muted" style="margin-top:6px">
            P=Precisão, R=Recall, F1=média harmônica, Jaccard=acerto por rótulo (TP/(TP+FP+FN)),
            Conc.Total=imagens idênticas ao gabarito, MAE=erro médio absoluto das avaliações.
            Métricas calculadas apenas sobre imagens que o usuário rotulou e que existem no gabarito.</div>
        </div>

        <div class="section">
          <h2>Gráficos</h2>
          <div class="grid2">
            <div class="chart-card" data-chart="acerto"><h4>Acerto por usuário (F1 %)</h4><div id="chartHit"></div></div>
            <div class="chart-card" data-chart="ranking"><h4>Ranking de proximidade ao gabarito (F1 %)</h4><div id="chartRank"></div></div>
            <div class="chart-card" data-chart="precisao_recall"><h4>Precisão x Recall por usuário (%)</h4><div id="chartPR"></div></div>
            <div class="chart-card" data-chart="falsos"><h4>Falsos positivos x falsos negativos</h4><div id="chartFPFN"></div></div>
            <div class="chart-card" data-chart="concordancia"><h4>Concordância total x parcial por usuário (%)</h4><div id="chartConc"></div></div>
            <div class="chart-card" data-chart="consenso_nao"><h4>Consenso - não passou (%)</h4><div id="chartConsensus"></div></div>
            <div class="chart-card" data-chart="consenso_sim"><h4>Consenso - passou (%)</h4><div id="chartConsensusPass"></div></div>
            <div class="chart-card" data-chart="cobertura"><h4>Imagens rotuladas por usuário</h4><div id="chartCov"></div></div>
            <div class="chart-card" data-chart="ocorrencia_erro"><h4>Ocorrência por tipo de erro (gabarito x usuários)</h4><div id="chartErr"></div></div>
            <div class="chart-card" data-chart="concordancia_erro"><h4>Concordância por tipo de erro (%)</h4><div id="chartErrAgree"></div></div>
            <div class="chart-card" data-chart="mae_avaliacao"><h4>Erro médio de avaliação (MAE) por usuário</h4><div id="chartMae"></div></div>
            <div class="chart-card" data-chart="distribuicao_avaliacao"><h4>Distribuição das avaliações (gabarito x usuários)</h4><div id="chartAval"></div></div>
          </div>
        </div>

        <div class="section">
          <h2>Erros por tipo</h2>
          <div class="grid2">
            <div class="tbl-wrap"><table class="tbl" id="tblOmit"></table></div>
            <div class="tbl-wrap"><table class="tbl" id="tblExtra"></table></div>
            <div class="tbl-wrap"><table class="tbl" id="tblBest"></table></div>
            <div class="tbl-wrap"><table class="tbl" id="tblWorst"></table></div>
          </div>
        </div>

        <div class="section">
          <h2>Matriz de confusão simplificada (gabarito → marcado pelo usuário)</h2>
          <div class="tbl-wrap" id="confMatrix"></div>
        </div>

        <div class="section">
          <h2>Concordância entre usuários <span id="interAvg" class="badge"></span></h2>
          <div class="small muted" style="margin-bottom:8px">Índice de Jaccard médio entre cada par de usuários (independe do gabarito).</div>
          <div class="tbl-wrap" id="interMatrix"></div>
        </div>

        <div class="section">
          <h2>Imagens com maior divergência entre usuários</h2>
          <div class="tbl-wrap"><table class="tbl" id="divImgs"></table></div>
        </div>

        <div class="section">
          <h2>Exportar</h2>
          <div class="exports">
            <button id="expJson" class="btn"><span class="ic"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></span> JSON</button>
            <button id="expPdf" class="btn"><span class="ic"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg></span> PDF</button>
          </div>
        </div>
        <div style="height:30px"></div>
      </div>
    </div>

  </div>
</div>

<div id="busy"><div class="spinner"></div></div>
<div id="toast"></div>
<audio id="easterAudio" loop preload="auto"></audio>

<script>
const BLANK="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";
let DATA=null, STATUS=null, view=[], idx=0, mode="alpha", focusUser="__all__";
const MODE_LABEL={alpha:"Calibrada", r:"Segmentada", rgba:"RGBA", rgb:"RGB", g:"G", b:"B"};

let activeTab="compare", imgToken=0, statsDirty=true;
let userSort={key:"f1",dir:-1};
let zoom=1, imgNatural={w:0,h:0}, imgDisplay={w:0,h:0}, panState=null;
let anonymizeUsers=false, userAliasMap={};

let easterActive=false;
let easterAssetsCache={};
let currentEaster=null;
let pendingZoomAfterEaster=null;
let skipNextZoomBlur=false;

const EASTER_ZOOM_CODES={
  678:true,
  110:true,
  83:true
};

const ZOOM_MIN=0.5, ZOOM_MAX=8, ZOOM_STEP=1.25;

const el=id=>document.getElementById(id);
const esc=s=>(s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const pct=x=>(x==null?"-":(x*100).toFixed(1)+"%");
const countPct=(n,total,rate)=>!total?((n||0)+"/0"):((n||0)+"/"+total+" ("+((rate==null?(n||0)/total:rate)*100).toFixed(1)+"%)");
const fix=(x,n)=>(x==null?"-":Number(x).toFixed(n==null?2:n));
function rebuildUserAliases(){
  userAliasMap={};
  ((DATA&&DATA.users)||[]).forEach((u,i)=>{userAliasMap[u.usuario]="Usuario "+(i+1);});
}
function userLabel(name){return anonymizeUsers?(userAliasMap[name]||name):name;}

function toast(msg,type){const t=el("toast");t.textContent=msg;t.className="show "+(type||"");
  clearTimeout(t._t);t._t=setTimeout(()=>t.className="",2600);}
function busy(on){el("busy").classList.toggle("show",!!on);}

function fallbackCopyText(text){
  const ta=document.createElement("textarea");
  ta.value=text;
  ta.setAttribute("readonly","");
  ta.style.position="fixed";
  ta.style.left="-9999px";
  ta.style.top="0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok=false;
  try{ok=document.execCommand("copy");}catch(e){ok=false;}
  document.body.removeChild(ta);
  return ok;
}

async function copyText(text){
  text=text==null?"":String(text);
  if(!text)return;
  let ok=false;
  if(navigator.clipboard&&navigator.clipboard.writeText){
    try{
      await navigator.clipboard.writeText(text);
      ok=true;
    }catch(e){ok=false;}
  }
  if(!ok)ok=fallbackCopyText(text);
  toast(ok?"Nome da imagem copiado":"Não consegui copiar",ok?"good":"bad");
}

function bridgeReady(method){
  if(!(window.pywebview&&window.pywebview.api))return false;
  if(window.pywebview.platform==="qtwebengine"&&!window.pywebview._QWebChannel)return false;
  return method?!!window.pywebview.api[method]:true;
}

function waitBridge(method,timeout=8000){
  const start=Date.now();
  return new Promise((resolve,reject)=>{
    function tick(){
      if(bridgeReady(method)){resolve();return;}
      if(Date.now()-start>=timeout){reject(new Error("API indisponivel: "+(method||"pywebview")));return;}
      setTimeout(tick,80);
    }
    tick();
  });
}

async function api(method,...args){
  await waitBridge(method);
  return await window.pywebview.api[method](...args);
}

async function ensureEasterAssets(code){
  const key=String(code);

  if(easterAssetsCache[key]){
    return easterAssetsCache[key];
  }

  const r = await api("get_easter_assets", key);

  if(!r.ok){
    throw new Error(r.error || "Falha ao carregar assets do easter egg");
  }

  easterAssetsCache[key]={
    code:key,
    name:r.name,
    type:r.type || (r.video ? "video" : "image"),
    image:r.image || null,
    audio:r.audio || null,
    video:r.video_url || r.video || null,
    videoFallback:r.video_url ? (r.video || null) : null,
    text:r.text || "",
    topText:r.top_text || "",
    bottomText:r.bottom_text || ""
  };

  return easterAssetsCache[key];
}

function stopEasterAudio(){
  const a = el("easterAudio");
  if(!a) return;

  try{
    a.pause();
    a.currentTime = 0;
  }catch(e){}
}

function setEasterOverlayText(assets, fallbackText){
  const top=el("easterTextTop");
  const bottom=el("easterTextBottom");
  if(!top || !bottom)return;

  const raw=String((assets&&assets.text)||fallbackText||"VAMOO GR\u00caMIOO").trim();
  const parts=raw.split(/\s+/);
  const topText=(assets&&assets.topText)||parts[0]||"";
  const bottomText=(assets&&assets.bottomText)||parts.slice(1).join(" ")||"";

  top.textContent=topText;
  bottom.textContent=bottomText;
  top.style.display=topText?"block":"none";
  bottom.style.display=bottomText?"block":"none";
}

function stopEasterVideo(){
  const v = el("easterVideo");
  const t = el("easterText");
  const wrap = el("imgWrap");
  const img = el("imgEl");

  if(wrap) wrap.classList.remove("easter-video");
  if(wrap) wrap.classList.remove("easter-celebration");
  document.body.classList.remove("easter-video-active");
  document.body.classList.remove("easter-celebration-active");
  if(t){
    const top=el("easterTextTop");
    const bottom=el("easterTextBottom");
    if(top)top.textContent="";
    if(bottom)bottom.textContent="";
  }
  if(img) img.style.opacity = "";

  if(!v) return;

  try{
    v.onloadedmetadata=null;
    v.onerror=null;
    v.pause();
    v.currentTime = 0;
    v.removeAttribute("src");
    v.removeAttribute("autoplay");
    v.removeAttribute("muted");
    v.load();
  }catch(e){}
}

function setPreparedVisibleVideo(src,w,h,text){
  const wrap=el("imgWrap");
  const img=el("imgEl");
  const video=el("easterVideo");

  wrap.classList.remove("noimg");
  wrap.classList.add("easter-video");
  document.body.classList.add("easter-video-active");

  img.onload=null;
  img.src=BLANK;
  img.style.opacity="0";

  imgNatural={w:w||1280,h:h||720};
  const next=calcImageDisplay();
  imgDisplay=next;
  img.style.width=next.w+"px";
  img.style.height=next.h+"px";

  setEasterOverlayText({text:text||"VAMOO GR\u00caMIOO"}, text);

  if(video.src!==src){
    video.src=src;
  }
  video.loop=true;
  video.autoplay=true;
  video.muted=true;
  video.defaultMuted=true;
  video.playsInline=true;
  video.controls=false;
  video.setAttribute("autoplay","");
  video.setAttribute("muted","");
  video.setAttribute("playsinline","");

  applyZoom(false);

  try{
    video.load();
    video.currentTime=0;
    video.play().catch(()=>{video.controls=true;});
  }catch(e){}
}

function renderEasterImage(){
  const assets=currentEaster;

  if(!assets || !assets.image)return;

  const wrap=el("imgWrap");

  stopEasterVideo();
  wrap.classList.remove("noimg");
  if(assets.text){
    wrap.classList.add("easter-celebration");
    document.body.classList.add("easter-celebration-active");
    setEasterOverlayText(assets);
  }
  el("imgName").textContent=assets.name||"Easter egg";
  el("imgInfo").textContent=assets.text||"Aos maiores de todos os tempos!!!";
  el("counter").textContent=(view.length ? idx+1 : 0)+" / "+view.length;

  const codeAtStart=assets.code;

  preloadImageDataUrl(assets.image).then(ready=>{
    if(!easterActive)return;
    if(!currentEaster || currentEaster.code!==codeAtStart)return;

    if(!ready.ok){
      stopEasterVideo();
      toast("Falha ao carregar imagem do easter egg","bad");
      return;
    }

    zoom=1;
    setPreparedVisibleImage(
      assets.image,
      ready.w||1,
      ready.h||1
    );
  });
}

function renderEasterVideo(){
  const assets=currentEaster;

  if(!assets || !assets.video)return;

  el("imgName").textContent=assets.name||"Easter egg";
  el("imgInfo").textContent=assets.text||"VAMOO GR\u00caMIOO";
  el("counter").textContent=(view.length ? idx+1 : 0)+" / "+view.length;

  const codeAtStart=assets.code;
  const video=el("easterVideo");
  assets._fallbackTried=false;

  if(video){
    video.onloadedmetadata=()=>{
      if(!easterActive)return;
      if(!currentEaster || currentEaster.code!==codeAtStart)return;
      imgNatural={
        w:video.videoWidth||1280,
        h:video.videoHeight||720
      };
      applyZoom(false);
    };
    video.onerror=()=>{
      if(!easterActive)return;
      if(!currentEaster || currentEaster.code!==codeAtStart)return;
      if(assets.videoFallback && !assets._fallbackTried){
        assets._fallbackTried=true;
        setPreparedVisibleVideo(
          assets.videoFallback,
          1280,
          720,
          assets.text||"VAMOO GR\u00caMIOO"
        );
        return;
      }
      if(easterActive && currentEaster && currentEaster.code===codeAtStart){
        toast("Falha ao carregar video do easter egg","bad");
      }
    };
  }

  zoom=1;
  setPreparedVisibleVideo(
    assets.video,
    1280,
    720,
    assets.text||"VAMOO GR\u00caMIOO"
  );
}

function renderEaster(){
  if(currentEaster && currentEaster.type==="video"){
    renderEasterVideo();
  }else{
    renderEasterImage();
  }
}

async function activateEasterEgg(code){
  try{
    const assets = await ensureEasterAssets(code);

    easterActive = true;
    currentEaster = assets;
    pendingZoomAfterEaster = null;

    if(assets.type==="video"){
      stopEasterAudio();
    }
    renderEaster();

    const a = el("easterAudio");

    if(a && assets.audio){
      try{
        a.pause();
        a.currentTime = 0;
      }catch(e){}

      if(a.src !== assets.audio){
        a.src = assets.audio;
      }

      a.loop = true;

      try{
        await a.play();
      }catch(e){}
    }
  }catch(e){
    easterActive = false;
    currentEaster = null;
    stopEasterAudio();
    stopEasterVideo();
    toast(e.message || "Falha ao ativar o easter egg","bad");
    loadImage();
  }
}

function deactivateEasterEgg(reloadNormalImage=true){
  const audio=el("easterAudio");
  const video=el("easterVideo");
  if(!easterActive && !(audio&&audio.src) && !(video&&video.src)) return;

  easterActive = false;
  currentEaster = null;
  stopEasterAudio();
  stopEasterVideo();

  const img = el("imgEl");
  if(img) img.onload = null;

  if(reloadNormalImage){
    loadImage();
  }else{
    updateZoomLabel();
  }
}

/* ---------------- boot ---------------- */
let booted=false;
let windowControlsWired=false;
function scheduleBoot(){
  if(booted)return;
  if(!bridgeReady()){setTimeout(scheduleBoot,80);return;}
  booted=true; wire(); refreshAll();
}
function wireWindowControls(){
  if(windowControlsWired)return;
  if(!el("btnClose")||!el("btnMin"))return;
  windowControlsWired=true;
  el("btnMin").onclick=()=>api("win_minimize").catch(()=>{});
  const closeApp=e=>{
    if(e)e.preventDefault();
    api("win_close").catch(()=>{try{window.close();}catch(_e){}});
  };
  el("btnClose").onpointerdown=closeApp;
  el("btnClose").onmousedown=closeApp;
  el("btnClose").onclick=closeApp;
}
function boot(){ scheduleBoot(); }
window.addEventListener("pywebviewready", scheduleBoot);
window.addEventListener("DOMContentLoaded", ()=>{ wireWindowControls(); scheduleBoot(); });
setTimeout(()=>{ wireWindowControls(); scheduleBoot(); }, 200);

function wire(){
  wireWindowControls();
  el("btnRefresh").onclick=async()=>{
    busy(true);
    try{
        if(easterActive) deactivateEasterEgg(false);
        resetImageView();
        await api("refresh");
        await refreshAll();
        toast("Tela atualizada","good");
    }catch(e){
        toast(e.message,"bad");
    }
    busy(false);
    };
  el("btnOrig").onclick=()=>pick("pick_original","Original");
  el("btnUsers").onclick=()=>pick("pick_users_folder","Usuários");
  el("btnZip").onclick=()=>pick("pick_zip","ZIP");
  el("btnSession").onclick=restoreSession;
  el("btnWarn").onclick=showWarnings;
  el("anonUsers").onchange=e=>{
    anonymizeUsers=e.target.checked;
    if(DATA){rebuildUserAliases();buildFilters();applyFilters();
      if(activeTab==="stats"){renderStats();statsDirty=false;} else statsDirty=true;}
  };

  document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>setTab(t.dataset.tab));
  document.querySelectorAll(".modebtn").forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
  el("zoomOut").onclick=()=>setZoom(zoom/ZOOM_STEP);
  el("zoomIn").onclick=()=>setZoom(zoom*ZOOM_STEP);
  el("zoomPct").addEventListener("focus",onZoomInputFocus);
  el("zoomPct").addEventListener("input",onZoomInputTyping);
  el("zoomPct").addEventListener("keydown",onZoomInputKeyDown);
  el("zoomPct").addEventListener("blur",()=>{
  if(skipNextZoomBlur){
    skipNextZoomBlur=false;
    return;
  }

  commitZoomInput(false);
});
  el("imgStage").addEventListener("wheel",onImageWheel,{passive:false});
  el("imgStage").addEventListener("mousedown",startPan);
  window.addEventListener("mousemove",movePan);
  window.addEventListener("mouseup",endPan);
  window.addEventListener("resize",()=>applyZoom(false));
  el("btnPrev").onclick=()=>go(-1);
  el("btnNext").onclick=()=>go(1);
  el("search").oninput=applyFilters;
  el("selUser").onchange=e=>{focusUser=e.target.value;applyFilters();};
  el("selError").onchange=applyFilters;
  el("fDiv").onchange=applyFilters;
  el("fAgree").onchange=applyFilters;

  el("expJson").onclick=exportJson;
  el("expPdf").onclick=exportPdf;

  document.addEventListener("keydown",e=>{
    if(activeTab!=="compare")return;
    const tag=(e.target.tagName||"").toLowerCase();
    if(tag==="input"||tag==="select")return;
    if(e.key==="ArrowLeft")go(-1);
    else if(e.key==="ArrowRight")go(1);
    else if(e.key==="+"||e.key==="=")setZoom(zoom*ZOOM_STEP);
    else if(e.key==="-")setZoom(zoom/ZOOM_STEP);
    else if(e.key==="0")resetImageView();
  });
}

async function pick(method,label){
  busy(true);
  try{
    const r=await api(method);
    if(r.cancelled){busy(false);return;}
    if(!r.ok){toast(label+": "+(r.error||"falhou"),"bad");busy(false);return;}
    if(method==="pick_original")toast("Gabarito: "+r.images+" imagens","good");
    else if(method==="pick_users_folder")toast(r.users.length+" usuário(s) carregados","good");
    else toast("ZIP: "+r.images+" imagens","good");
    await refreshAll();
  }catch(e){toast(e.message,"bad");}
  busy(false);
}

async function restoreSession(){
  busy(true);
  try{
    const r=await api("restore_last_session");
    if(!r.ok){
      toast(r.error||"Nao encontrei sessao anterior","bad");
      busy(false);
      return;
    }
    await refreshAll();
    const loaded=r.loaded||{};
    const parts=[];
    if(loaded.original_images!=null)parts.push(loaded.original_images+" gabarito");
    if(loaded.users_count!=null)parts.push(loaded.users_count+" usuario(s)");
    if(loaded.zip_images!=null)parts.push(loaded.zip_images+" imagens ZIP");
    const suffix=parts.length?": "+parts.join(", "):"";
    toast((r.problems&&r.problems.length)?
      ("Sessao restaurada com avisos"+suffix):
      ("Sessao anterior restaurada"+suffix),"good");
  }catch(e){
    toast(e.message,"bad");
  }
  busy(false);
}

async function refreshAll(){
  try{ STATUS=await api("get_status"); updateChips(); }catch(e){}
  let r;
  try{ r=await api("get_comparison"); }catch(e){ showEmpty(); return; }
  if(r.ok){
    DATA=r.data; STATUS=r.status||STATUS; updateChips();
    rebuildUserAliases();
    buildFilters(); statsDirty=true;
    applyFilters();
    if(activeTab==="stats"){renderStats();statsDirty=false;}
  }else{ DATA=null; showEmpty(); }
}
function showEmpty(){
  resetImageView();
  el("counter").textContent="0 / 0"; el("imgEl").src=BLANK;
  el("filterInfo").textContent="";
  ["metaPanel","origPanel","usersPanel"].forEach(id=>{const e=el(id); if(e)e.innerHTML='<span class="muted">—</span>';});
}

function updateChips(){
  const s=STATUS||{};
  const co=el("chipOrig"),cu=el("chipUsers"),cz=el("chipZip");
  co.classList.toggle("on",!!s.original_loaded);
  co.lastChild.textContent= s.original_loaded? ("Gabarito · "+s.original_images+" imagens"):"Sem gabarito";
  cu.classList.toggle("on",(s.users_count||0)>0);
  cu.lastChild.textContent=(s.users_count||0)+" Usuário(s)";
  cz.classList.toggle("on",(s.zip_images||0)>0);
  cz.lastChild.textContent= s.zip_images? ("Zip · "+s.zip_images+" imagens"):"Sem zip";
  el("warnCount").textContent=(s.warnings||[]).length;
}

function showWarnings(){
  const w=(STATUS&&STATUS.warnings)||[];
  alert(w.length? ("Avisos / diagnóstico ("+w.length+"):\n\n"+w.join("\n")) : "Nenhum aviso. Tudo certo!");
}

/* ---------------- tabs ---------------- */
function setTab(t){
  activeTab=t;
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x.dataset.tab===t));
  el("view-compare").classList.toggle("active",t==="compare");
  el("view-stats").classList.toggle("active",t==="stats");
  if(t==="stats"&&DATA){ renderStats(); statsDirty=false; }
}

function setMode(nextMode){
  if(!nextMode)return;
  const changed=mode!==nextMode;
  mode=nextMode;
  document.querySelectorAll(".modebtn").forEach(b=>b.classList.toggle("active",b.dataset.mode===mode));
  if(!changed)return;
  resetImageView();
  if(easterActive){
    deactivateEasterEgg(true);
  }else{
    loadImage();
  }
}

/* ---------------- filters ---------------- */
function buildFilters(){
  const su=el("selUser"); const cur=su.value;
  su.innerHTML='<option value="__all__">Todos</option>'+
    DATA.users.map(u=>`<option value="${esc(u.usuario)}">${esc(userLabel(u.usuario))}</option>`).join("");
  su.value=[...su.options].some(o=>o.value===cur)?cur:"__all__"; focusUser=su.value;
  const se=el("selError"); const cure=se.value;
  se.innerHTML='<option value="__all__">Todos</option>'+
    DATA.error_labels.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join("");
  se.value=[...se.options].some(o=>o.value===cure)?cure:"__all__";
}

function applyFilters(){
  if(!DATA){return;}
  const q=el("search").value.trim().toLowerCase();
  const u=focusUser, et=el("selError").value;
  const onlyDiv=el("fDiv").checked, onlyAgree=el("fAgree").checked;
  const prevBase=view[idx]?view[idx].base:null;
  view=DATA.images.filter(im=>{
    if(q && !((im.meta.arquivo||"").toLowerCase().includes(q)))return false;
    if(et!=="__all__"){
      let has=im.original_erros.some(e=>e.nome===et);
      if(!has){for(const k in im.users){const ud=im.users[k];
        if(ud.erros&&ud.erros.some(e=>e.nome===et)){has=true;break;}}}
      if(!has)return false;
    }
    if(u!=="__all__"){
      const ud=im.users[u];
      if(!ud||!ud.labeled)return false;
      if(ud.no_reference){return !onlyDiv&&!onlyAgree;}
      if(onlyDiv&&ud.exact)return false;
      if(onlyAgree&&!ud.exact)return false;
    }else{
      if(onlyDiv&&!im.divergence)return false;
      if(onlyAgree&&!im.full_agreement)return false;
    }
    return true;
  });
  const np=view.findIndex(im=>im.base===prevBase);
  idx=np>=0?np:0;
  if(idx>=view.length)idx=Math.max(0,view.length-1);
  if((view[idx]?view[idx].base:null)!==prevBase)resetImageView();
  el("filterInfo").textContent=view.length+" de "+DATA.images.length+" imagens";
  renderCompare();
}

/* ---------------- compare render ---------------- */
function go(d){ if(!view.length)return;
  const next=Math.min(view.length-1,Math.max(0,idx+d));
  if(next!==idx){idx=next;resetImageView();}
  renderCompare();
}

function clamp(v,min,max){return Math.max(min,Math.min(max,v));}
function updateZoomLabel(){
  const has=!!(imgNatural.w&&imgNatural.h);
  el("zoomPct").value=String(Math.round(zoom*100))+"%";
  el("zoomPct").disabled=!has;
  el("zoomOut").disabled=!has||zoom<=ZOOM_MIN+.001;
  el("zoomIn").disabled=!has||zoom>=ZOOM_MAX-.001;
  el("imgStage").classList.toggle("zoomed",has&&zoom>1.01);
}
function parseZoomInputValue(raw){
  const n=parseFloat(String(raw||"").replace(/[^\d]/g,""));
  if(!Number.isFinite(n))return null;
  return Math.min(n,800)/100;
}
function onZoomInputTyping(e){
  let digits=String(e.target.value||"").replace(/[^\d]/g,"").slice(0,3);
  if(digits){
    const n=Math.min(parseInt(digits,10)||0,800);
    digits=String(n);
  }
  e.target.value=digits;
}
function onZoomInputFocus(e){
  e.target.value=String(Math.round(zoom*100));
  requestAnimationFrame(()=>e.target.select());
}
async function commitZoomInput(fromEnter=false){
  const rawDigits = String(el("zoomPct").value || "").replace(/[^\d]/g,"");
  const typedInt = rawDigits ? parseInt(rawDigits,10) : null;

  /*
    678 e 110 continuam secretos via Enter.
    O 83 tambem dispara quando o campo perde foco depois de digitar 83.
  */
  if(typedInt != null && EASTER_ZOOM_CODES[typedInt] &&
      (fromEnter || typedInt===83)){
    await activateEasterEgg(typedInt);
    return;
  }

  if(easterActive){
    pendingZoomAfterEaster = null;
    deactivateEasterEgg(true);
  }

  const parsed = parseZoomInputValue(rawDigits);

  if(parsed == null){
    updateZoomLabel();
    return;
  }

  setZoom(parsed);
}
async function onZoomInputKeyDown(e){
  if(e.key==="Enter"){
    e.preventDefault();

    skipNextZoomBlur=true;
    await commitZoomInput(true);

    e.target.blur();
  }else if(e.key==="Escape"){
    updateZoomLabel();
    e.target.blur();
  }
}
function calcImageDisplay(){
  if(!imgNatural.w||!imgNatural.h)return {w:0,h:0};
  const stage=el("imgStage");
  const maxW=Math.max(1,stage.clientWidth-28);
  const maxH=Math.max(1,stage.clientHeight-28);
  const canUpscale=!!(easterActive && currentEaster &&
    currentEaster.type==="gif");
  const fit=canUpscale?
    Math.min(maxW/imgNatural.w,maxH/imgNatural.h):
    Math.min(1,maxW/imgNatural.w,maxH/imgNatural.h);
  return {
    w:Math.max(1,Math.round(imgNatural.w*fit*zoom)),
    h:Math.max(1,Math.round(imgNatural.h*fit*zoom))
  };
}
function applyZoom(keepCenter){
  const img=el("imgEl"), stage=el("imgStage");
  updateZoomLabel();
  if(!imgNatural.w||!imgNatural.h){
    img.style.width=""; img.style.height="";
    imgDisplay={w:0,h:0}; return;
  }
  let fx=.5,fy=.5;
  if(keepCenter&&imgDisplay.w&&imgDisplay.h){
    const sw=stage.scrollWidth,sh=stage.scrollHeight;
    const ix=(sw-imgDisplay.w)/2,iy=(sh-imgDisplay.h)/2;
    fx=clamp((stage.scrollLeft+stage.clientWidth/2-ix)/imgDisplay.w,0,1);
    fy=clamp((stage.scrollTop+stage.clientHeight/2-iy)/imgDisplay.h,0,1);
  }
  const next=calcImageDisplay();
  imgDisplay=next;
  img.style.width=next.w+"px";
  img.style.height=next.h+"px";
  requestAnimationFrame(()=>{
    const sw=stage.scrollWidth,sh=stage.scrollHeight;
    const ix=(sw-next.w)/2,iy=(sh-next.h)/2;
    stage.scrollLeft=Math.max(0,ix+fx*next.w-stage.clientWidth/2);
    stage.scrollTop=Math.max(0,iy+fy*next.h-stage.clientHeight/2);
  });
}
function setZoom(value){
  const newZoom = clamp(value, ZOOM_MIN, ZOOM_MAX);

  if(easterActive){
    pendingZoomAfterEaster = newZoom;
    deactivateEasterEgg(true);
    return;
  }

  zoom = newZoom;
  applyZoom(true);
}
function resetImageView(){zoom=1;endPan();applyZoom(false);}
function onImageWheel(e){
  if(!imgNatural.w||!imgNatural.h)return;
  e.preventDefault();
  setZoom(e.deltaY<0?zoom*ZOOM_STEP:zoom/ZOOM_STEP);
}
function startPan(e){
  if(e.button!==0||!imgNatural.w)return;
  const stage=el("imgStage");
  if(stage.scrollWidth<=stage.clientWidth&&stage.scrollHeight<=stage.clientHeight)return;
  panState={x:e.clientX,y:e.clientY,left:stage.scrollLeft,top:stage.scrollTop};
  stage.classList.add("panning");
  e.preventDefault();
}
function movePan(e){
  if(!panState)return;
  const stage=el("imgStage");
  stage.scrollLeft=panState.left-(e.clientX-panState.x);
  stage.scrollTop=panState.top-(e.clientY-panState.y);
}
function endPan(){
  panState=null;
  const stage=el("imgStage");
  if(stage)stage.classList.remove("panning");
}
function preloadImageDataUrl(src){
  return new Promise(resolve=>{
    const probe=new Image();

    probe.onload=()=>resolve({
      ok:true,
      w:probe.naturalWidth||0,
      h:probe.naturalHeight||0
    });

    probe.onerror=()=>resolve({
      ok:false,
      w:0,
      h:0
    });

    probe.src=src;
  });
}

function setPreparedVisibleImage(src,w,h){
  const img=el("imgEl");

  imgNatural={
    w:w||1,
    h:h||1
  };

  const next=calcImageDisplay();
  imgDisplay=next;

  /*
    Primeiro define o tamanho final.
    Só depois troca o src visível.
    Isso evita a imagem aparecer pequena e depois crescer.
  */
  img.style.width=next.w+"px";
  img.style.height=next.h+"px";
  img.src=src;

  applyZoom(false);
}

function renderCompare(){
  el("counter").textContent=(view.length?idx+1:0)+" / "+view.length;
  el("btnPrev").disabled=idx<=0; el("btnNext").disabled=idx>=view.length-1;
  const im=view[idx];
  if(!im){ el("metaPanel").innerHTML='<span class="muted">Nenhuma imagem para os filtros atuais.</span>';
    el("origPanel").innerHTML=""; el("usersPanel").innerHTML=""; el("imgEl").src=BLANK;
    el("imgName").textContent="—"; el("imgInfo").textContent=""; return; }
  renderMeta(im); renderOrig(im); renderUsers(im); loadImage();
}

function renderMeta(im){
  const m=im.meta;
  const rows=[["Arquivo",m.arquivo],["Data",m.data],["ID",m.id],["Dedo",m.dedo],
    ["Grupo",m.grupo]];
  if(im.dupes)rows.push(["Duplicatas",im.dupes]);
  el("metaPanel").innerHTML=rows.map(r=>`<span class="k">${esc(r[0])}</span><span>${esc(r[1]==null?"—":r[1])}</span>`).join("");
}

function renderOrig(im){
  if(!im.has_reference){el("origPanel").innerHTML='<span class="muted">Imagem sem gabarito (não está no JSON original).</span>';return;}
  if(!im.original_erros.length){el("origPanel").innerHTML='<span class="muted">Sem erros no gabarito (imagem limpa).</span>';return;}
  el("origPanel").innerHTML=im.original_erros.map(e=>
    `<span class="chipx neutral" title="${esc(e.descricao||"")}">${esc(e.nome)}${e.avaliacao!=null?`<span class="av">${e.avaliacao}</span>`:""}</span>`).join("");
}

function renderUsers(im){
  const host=el("usersPanel"); host.innerHTML="";
  let names=DATA.users.map(u=>u.usuario);
  if(focusUser!=="__all__")names=names.filter(n=>n===focusUser);
  if(!names.length){host.innerHTML='<span class="muted">Nenhum usuário carregado.</span>';return;}
  names.forEach(name=>{
    const ud=im.users[name]||{labeled:false};
    const dname=userLabel(name);
    const card=document.createElement("div"); card.className="ucard";
    if(!ud.labeled){card.classList.add("nolabel");
      card.innerHTML=`<div class="ucard-h"><b>${esc(dname)}</b><span class="badge">sem rotulagem p/ esta imagem</span></div>`;
      host.appendChild(card);return;}
    if(ud.no_reference){
      card.innerHTML=`<div class="ucard-h"><b>${esc(dname)}</b><span class="badge div">Sem gabarito p/ comparar</span></div>`+
        `<div class="chips">${ud.erros.map(e=>`<span class="chipx neutral" title="${esc(e.descricao||"")}">${esc(e.nome)}${e.avaliacao!=null?`<span class="av">${e.avaliacao}</span>`:""}</span>`).join("")||'<span class="muted">sem erros</span>'}</div>`;
      host.appendChild(card);return;}
    const badge=ud.exact?'<span class="badge good">concordância total</span>':
      (ud.partial?'<span class="badge warn">parcial</span>':'<span class="badge bad">divergência</span>');
    let h=`<div class="ucard-h"><b>${esc(dname)}</b>${badge}<span class="badge">Jaccard ${(ud.jaccard*100).toFixed(0)}%</span>`+
      `<span class="badge good">${ud.tp.length} ok</span><span class="badge bad">${ud.fp.length} extra</span><span class="badge warn">${ud.fn.length} faltou</span></div>`;
    h+='<div class="chips">';
    if(!ud.erros.length && !ud.fn.length) h+='<span class="muted">marcou imagem como limpa</span>';
    ud.erros.forEach(e=>{ const cls=e.status==="tp"?"tp":"fp";
      h+=`<span class="chipx ${cls}" title="${esc(e.descricao||"")}">${esc(e.nome)}${e.avaliacao!=null?`<span class="av">${e.avaliacao}</span>`:""}</span>`;});
    ud.fn.forEach(n=>{ h+=`<span class="chipx fn">faltou: ${esc(n)}</span>`; });
    h+='</div>';
    if(ud.aval_diffs&&ud.aval_diffs.length){
      h+='<div class="avaldiff">'+ud.aval_diffs.map(d=>
        `<span class="adiff ${d.diff>0?"on":""}">${esc(d.erro)}: ${d.original}→${d.usuario} (Δ${d.diff})</span>`).join("")+'</div>';}
    card.innerHTML=h; host.appendChild(card);
  });
}

async function loadImage(){
  if(easterActive){
    renderEaster();
    return;
  }

  const im=view[idx];
  const wrap=el("imgWrap");
  const img=el("imgEl");

  if(!im){
    img.onload=null;
    imgNatural={w:0,h:0};
    pendingZoomAfterEaster=null;
    applyZoom(false);
    img.src=BLANK;
    return;
  }

  el("imgName").textContent=im.meta.arquivo||im.base;

  if(!im.in_zip){
    wrap.classList.add("noimg");
    img.onload=null;
    img.src=BLANK;
    imgNatural={w:0,h:0};
    pendingZoomAfterEaster=null;
    applyZoom(false);
    el("imgInfo").textContent="imagem ausente no ZIP";
    return;
  }

  wrap.classList.remove("noimg");
  el("imgInfo").textContent="carregando...";

  const token=++imgToken;

  try{
    const r=await api("get_image",im.base,mode);

    if(token!==imgToken)return;

    if(r.ok){
      /*
        Pré-carrega a imagem antes de trocar o src visível.
        Enquanto isso, a imagem anterior continua na tela.
      */
      const ready=await preloadImageDataUrl(r.data);

      if(token!==imgToken)return;

      if(!ready.ok){
        imgNatural={w:0,h:0};
        pendingZoomAfterEaster=null;
        applyZoom(false);
        img.src=BLANK;
        el("imgInfo").textContent="falha ao decodificar imagem";
        return;
      }

      if(pendingZoomAfterEaster!=null){
        zoom=pendingZoomAfterEaster;
        pendingZoomAfterEaster=null;
      }

      const finalW=r.w||ready.w||1;
      const finalH=r.h||ready.h||1;

      setPreparedVisibleImage(r.data,finalW,finalH);

      el("imgInfo").textContent=finalW+"×"+finalH+"px · "+(MODE_LABEL[mode]||mode);
    }else{
      imgNatural={w:0,h:0};
      pendingZoomAfterEaster=null;
      applyZoom(false);
      img.src=BLANK;
      el("imgInfo").textContent=r.error||"erro";
    }
  }catch(e){
    if(token===imgToken){
      el("imgInfo").textContent=e.message;
    }
  }
}
/* ---------------- stats ---------------- */
function renderStats(){
  if(!DATA)return;
  const s=DATA.summary;
  const consensusTotal=s.consensus_scored_images||0;
  const consensusPassed=s.consensus_passed_images||0;
  const consensusFailed=s.consensus_failed_images||0;
  const consensusPassRate=s.consensus_pass_rate;
  const cards=[["Imagens",s.total_images],["Usuários",s.total_users],
    ["Erros no gabarito",s.total_original_errors],["Tipos de erro",s.error_types],
    ["Imagens divergentes",s.total_divergent_images],["Concordância total",s.total_full_agreement_images],
    ["Consenso médio",pct(s.consensus_mean_score)],
    ["Passou consenso",countPct(consensusPassed,consensusTotal,consensusPassRate)],
    ["Não passou consenso",countPct(consensusFailed,consensusTotal,consensusPassRate==null?null:1-consensusPassRate)]];
  el("cards").innerHTML=cards.map(c=>`<div class="statcard"><div class="v">${c[1]}</div><div class="l">${esc(c[0])}</div></div>`).join("");

  renderConsensus();
  renderUserTable();
  drawCharts();
  errTable("tblOmit","Erros mais omitidos","FN",DATA.most_omitted,e=>e.fn);
  errTable("tblExtra","Erros mais marcados indevidamente","FP",DATA.most_extra,e=>e.fp);
  errTable("tblBest","Maior concordância","Concord.",DATA.best_agreement,e=>pct(e.agreement));
  errTable("tblWorst","Maior divergência","Diverg.",DATA.worst_agreement,e=>pct(e.divergence));
  renderConfusion();
  renderInter();
  renderDivImgs();
}

function renderConsensus(){
  const s=DATA.summary||{};
  const scored=s.consensus_scored_images||0;
  el("consensusAvg").textContent=scored?
    ("média "+pct(s.consensus_mean_score)+" · passou "+(s.consensus_passed_images||0)+"/"+scored+" · não passou "+(s.consensus_failed_images||0)+"/"+scored):"";
  const d=DATA.images_low_consensus||[];
  const head='<tr><th>Arquivo</th><th class="num">Consenso</th><th class="num">Passou</th><th class="num">OK/Total</th><th class="num">Extra</th><th class="num">Faltou</th><th class="num">Usuários</th></tr>';
  const body=d.map(x=>`<tr><td><button type="button" class="copy-img-name" data-copy-image="${esc(x.arquivo)}" title="Copiar nome da imagem">${esc(x.arquivo)}</button></td><td class="num">${pct(x.score)}</td><td class="num">${x.passed?"sim":"não"}</td><td class="num">${esc(x.correct_votes)}/${esc(x.denominator)}</td><td class="num">${esc(x.extra_votes)}</td><td class="num">${esc(x.omitted_votes)}</td><td class="num">${esc(x.comparable_users)}</td></tr>`).join("")
    ||'<tr><td class="muted" colspan="7">sem imagens com gabarito e usuários comparáveis</td></tr>';
  el("tblConsensus").innerHTML=head+body;
  el("tblConsensus").querySelectorAll("[data-copy-image]").forEach(btn=>{
    btn.onclick=()=>copyText(btn.dataset.copyImage);
  });
}

function renderUserTable(){
  const cols=[["usuario","Usuário",0],["labeled_images","Imagens",1],["user_errors_total","Marcou",1],
    ["precision","P",2],["recall","R",2],["f1","F1",2],["jaccard","Jaccard",2],
    ["exact_rate","Conc.Tot",3],["partial_rate","Conc.Parc",3],
    ["fp","FP",1],["fn","FN",1],["aval_mae","MAE",4]];
  const rows=[...DATA.users].sort((a,b)=>{
    let va=userSort.key==="usuario"?userLabel(a.usuario):a[userSort.key];
    let vb=userSort.key==="usuario"?userLabel(b.usuario):b[userSort.key];
    if(va==null)va=-1; if(vb==null)vb=-1;
    return (va>vb?1:va<vb?-1:0)*userSort.dir;});
  const head="<tr>"+cols.map(c=>`<th class="${c[2]?'num':''}" data-key="${c[0]}">${esc(c[1])}${userSort.key===c[0]?(userSort.dir<0?" ▼":" ▲"):""}</th>`).join("")+"</tr>";
  const body=rows.map(u=>"<tr>"+cols.map(c=>{
    let v=u[c[0]];
    if(c[0]==="usuario")v=userLabel(v);
    if(c[2]===2)v=pct(v); else if(c[2]===3)v=pct(v); else if(c[2]===4)v=(v==null?"-":fix(v,2));
    return `<td class="${c[2]?'num':''}">${esc(v)}</td>`;}).join("")+"</tr>").join("");
  el("userTable").innerHTML=head+body;
  el("userTable").querySelectorAll("th").forEach(th=>th.onclick=()=>{
    const k=th.dataset.key;
    if(userSort.key===k)userSort.dir*=-1; else {userSort.key=k;userSort.dir=(k==="usuario")?1:-1;}
    renderUserTable();});
}

function errTable(id,title,col,data,valf){
  const head=`<tr><th>${esc(title)}</th><th class="num">${esc(col)}</th></tr>`;
  const body=(data||[]).map(e=>`<tr><td>${esc(e.nome)}</td><td class="num">${esc(valf(e))}</td></tr>`).join("")
    ||'<tr><td class="muted" colspan="2">sem dados</td></tr>';
  el(id).innerHTML=head+body;
}

function renderConfusion(){
  const c=DATA.confusion; const L=c.labels;
  if(!L.length){el("confMatrix").innerHTML='<div class="muted small" style="padding:12px">sem dados</div>';return;}
  let max=1; c.matrix.forEach(r=>r.forEach(v=>{if(v>max)max=v;}));
  let h='<table class="heat"><tr><th class="lbl">gabarito \\ usuário</th>'+L.map(n=>`<th>${esc(n)}</th>`).join("")+"</tr>";
  c.matrix.forEach((row,i)=>{ h+=`<tr><td class="lbl">${esc(L[i])}</td>`+
    row.map((v,j)=>{const bg=heatColor(v/max, i===j);
      return `<td style="background:${bg}">${v||""}</td>`;}).join("")+"</tr>";});
  h+="</table>"; el("confMatrix").innerHTML=h;
}

function renderInter(){
  const im=DATA.inter_user; const U=im.users;
  let sum=0,n=0; im.matrix.forEach((row,i)=>row.forEach((v,j)=>{if(i<j&&v!=null){sum+=v;n++;}}));
  el("interAvg").textContent=n?("média "+(sum/n*100).toFixed(0)+"%"):"";
  if(!U.length){el("interMatrix").innerHTML='<div class="muted small" style="padding:12px">sem usuários</div>';return;}
  let h='<table class="heat"><tr><th class="lbl"></th>'+U.map(n=>`<th>${esc(userLabel(n))}</th>`).join("")+"</tr>";
  im.matrix.forEach((row,i)=>{ h+=`<tr><td class="lbl">${esc(userLabel(U[i]))}</td>`+
    row.map((v,j)=>{ if(v==null)return '<td class="muted">–</td>';
      return `<td style="background:${heatColor(v,i===j)}">${(v*100).toFixed(0)}</td>`;}).join("")+"</tr>";});
  h+="</table>"; el("interMatrix").innerHTML=h;
}

function renderDivImgs(){
  const d=DATA.images_most_divergent||[];
  const head='<tr><th>Arquivo</th><th class="num">Concordância</th><th class="num">Usuários</th></tr>';
  const body=d.map(x=>`<tr><td>${esc(x.arquivo)}</td><td class="num">${pct(x.inter_user_agreement)}</td><td class="num">${x.comparable_users}</td></tr>`).join("")
    ||'<tr><td class="muted" colspan="3">sem dados (precisa de ≥2 usuários por imagem)</td></tr>';
  el("divImgs").innerHTML=head+body;
}

function heatColor(t,diag){
  t=Math.max(0,Math.min(1,t||0));
  if(diag)return `rgba(61,220,151,${0.12+0.55*t})`;
  return `rgba(255,123,114,${0.10+0.55*t})`;
}

/* ---------------- charts (SVG) ---------------- */
function svgBars(data,opts){
  opts=opts||{}; const W=opts.w||560,H=opts.h||300;
  const pad={l:44,r:14,t:14,b:70};
  const iw=W-pad.l-pad.r, ih=H-pad.t-pad.b;
  const max=opts.percent?100:Math.max(1,...data.map(d=>d.value));
  const bw=iw/Math.max(1,data.length);
  let g=`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">`;
  g+=`<rect width="${W}" height="${H}" fill="#161d2e"/>`;
  const dec=opts.decimals||0;
  const fmt=v=>opts.percent?v.toFixed(0):(dec?v.toFixed(dec):(""+v));
  for(let k=0;k<=4;k++){const y=pad.t+ih*k/4;const val=max*(1-k/4);
    g+=`<line x1="${pad.l}" y1="${y}" x2="${W-pad.r}" y2="${y}" stroke="#283250" stroke-width="1"/>`;
    g+=`<text x="${pad.l-6}" y="${y+3}" fill="#6b7a9c" font-size="9" text-anchor="end">${val.toFixed(dec)}</text>`;}
  data.forEach((d,i)=>{const h=ih*(d.value/max);const x=pad.l+i*bw+bw*0.18;const w=bw*0.64;const y=pad.t+ih-h;
    g+=`<rect x="${x}" y="${y}" width="${w}" height="${Math.max(0,h)}" rx="3" fill="${d.color||opts.color||'#6ea8fe'}"/>`;
    g+=`<text x="${x+w/2}" y="${y-4}" fill="#cdd7ee" font-size="9" text-anchor="middle">${fmt(d.value)}</text>`;
    g+=`<text x="${x+w/2}" y="${pad.t+ih+12}" fill="#90a0c0" font-size="9" text-anchor="end" transform="rotate(-35 ${x+w/2} ${pad.t+ih+12})">${escapeSvg(d.label)}</text>`;});
  g+="</svg>"; return g;
}

function svgGrouped(cats,series,opts){
  opts=opts||{}; const W=opts.w||560,H=opts.h||300;
  const pad={l:44,r:14,t:30,b:70};
  const iw=W-pad.l-pad.r, ih=H-pad.t-pad.b;
  const max=Math.max(1,...series.flatMap(s=>s.values));
  const gw=iw/Math.max(1,cats.length); const n=series.length; const bw=gw*0.7/n;
  let g=`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">`;
  g+=`<rect width="${W}" height="${H}" fill="#161d2e"/>`;
  series.forEach((s,si)=>{g+=`<rect x="${pad.l+si*92}" y="8" width="11" height="11" rx="2" fill="${s.color}"/>`+
    `<text x="${pad.l+si*92+16}" y="17" fill="#cdd7ee" font-size="10">${escapeSvg(s.name)}</text>`;});
  for(let k=0;k<=4;k++){const y=pad.t+ih*k/4;
    g+=`<line x1="${pad.l}" y1="${y}" x2="${W-pad.r}" y2="${y}" stroke="#283250"/>`;
    g+=`<text x="${pad.l-6}" y="${y+3}" fill="#6b7a9c" font-size="9" text-anchor="end">${(max*(1-k/4)).toFixed(0)}</text>`;}
  cats.forEach((c,i)=>{const x0=pad.l+i*gw+gw*0.15;
    series.forEach((s,si)=>{const v=s.values[i]||0;const h=ih*(v/max);const x=x0+si*bw;const y=pad.t+ih-h;
      g+=`<rect x="${x}" y="${y}" width="${bw*0.86}" height="${Math.max(0,h)}" rx="2" fill="${s.color}"/>`;
      if(v)g+=`<text x="${x+bw*0.43}" y="${y-3}" fill="#cdd7ee" font-size="8" text-anchor="middle">${v}</text>`;});
    g+=`<text x="${x0+gw*0.35}" y="${pad.t+ih+12}" fill="#90a0c0" font-size="9" text-anchor="end" transform="rotate(-35 ${x0+gw*0.35} ${pad.t+ih+12})">${escapeSvg(c)}</text>`;});
  g+="</svg>"; return g;
}

function svgHBars(data,opts){
  opts=opts||{}; const W=opts.w||560; const pad={l:120,r:40,t:10,b:10};
  const n=Math.max(1,data.length), minRowH=24;
  const H=Math.max(opts.h||300,pad.t+pad.b+n*minRowH);
  const rowH=(H-pad.t-pad.b)/n;
  const barH=Math.max(10,Math.min(30,rowH-12));
  const iw=W-pad.l-pad.r; const max=opts.percent?100:Math.max(1,...data.map(d=>d.value));
  let g=`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">`;
  g+=`<rect width="${W}" height="${H}" fill="#161d2e"/>`;
  data.forEach((d,i)=>{const y=pad.t+i*rowH, barY=y+(rowH-barH)/2; const w=iw*(d.value/max);
    g+=`<text x="${pad.l-8}" y="${y+rowH/2+3}" fill="#cdd7ee" font-size="10" text-anchor="end">${escapeSvg(d.label)}</text>`;
    g+=`<rect x="${pad.l}" y="${barY}" width="${iw}" height="${barH}" rx="3" fill="#1f2a44"/>`;
    g+=`<rect x="${pad.l}" y="${barY}" width="${Math.max(0,w)}" height="${barH}" rx="3" fill="${d.color||'#6ea8fe'}"/>`;
    g+=`<text x="${pad.l+Math.max(w,0)+6}" y="${y+rowH/2+3}" fill="#90a0c0" font-size="9">${opts.percent?d.value.toFixed(0)+'%':d.value}</text>`;});
  g+="</svg>"; return g;
}
function svgEmpty(label,opts){
  opts=opts||{}; const W=opts.w||560,H=opts.h||300;
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">`+
    `<rect width="${W}" height="${H}" fill="#161d2e"/>`+
    `<text x="${W/2}" y="${H/2}" fill="#90a0c0" font-size="13" text-anchor="middle">${escapeSvg(label||"sem dados")}</text>`+
    `</svg>`;
}
function escapeSvg(s){return (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}

function drawCharts(){
  const U=DATA.users;
  el("chartHit").innerHTML=svgBars(U.map(u=>({label:userLabel(u.usuario),value:u.f1*100,
    color:colorScale(u.f1)})),{percent:true});
  const rank=DATA.ranking.map(name=>U.find(u=>u.usuario===name)).filter(Boolean);
  el("chartRank").innerHTML=svgHBars(rank.map(u=>({label:userLabel(u.usuario),value:u.f1*100,
    color:colorScale(u.f1)})),{percent:true});
  el("chartPR").innerHTML=svgGrouped(U.map(u=>userLabel(u.usuario)),
    [{name:"Precisão",color:"#6ea8fe",values:U.map(u=>Math.round(u.precision*100))},
     {name:"Recall",color:"#3ddc97",values:U.map(u=>Math.round(u.recall*100))}],{percent:true});
  el("chartFPFN").innerHTML=svgGrouped(U.map(u=>userLabel(u.usuario)),
    [{name:"Falsos positivos",color:"#ff7b72",values:U.map(u=>u.fp)},
     {name:"Falsos negativos",color:"#ffb454",values:U.map(u=>u.fn)}]);
  el("chartConc").innerHTML=svgGrouped(U.map(u=>userLabel(u.usuario)),
    [{name:"Total",color:"#3ddc97",values:U.map(u=>Math.round(u.exact_rate*100))},
     {name:"Parcial",color:"#ffb454",values:U.map(u=>Math.round(u.partial_rate*100))}],{percent:true});
  const cNo=[...(DATA.images_failed_consensus||[])].slice(0,15);
  el("chartConsensus").innerHTML=cNo.length?svgHBars(cNo.map(x=>({label:x.arquivo,value:(x.score||0)*100,
    color:"#ff7b72"})),{percent:true}):svgEmpty("nenhuma imagem reprovada");
  const cYes=[...(DATA.images_passed_consensus||[])].slice(0,15);
  el("chartConsensusPass").innerHTML=cYes.length?svgHBars(cYes.map(x=>({label:x.arquivo,value:(x.score||0)*100,
    color:colorScale(x.score||0)})),{percent:true}):svgEmpty("nenhuma imagem aprovada");
  el("chartCov").innerHTML=svgBars(U.map(u=>({label:userLabel(u.usuario),value:u.labeled_images,
    color:"#8bb4ff"})),{});
  const errs=[...DATA.errors].sort((a,b)=>(b.orig_count+b.user_count)-(a.orig_count+a.user_count)).slice(0,12);
  el("chartErr").innerHTML=svgGrouped(errs.map(e=>e.nome),
    [{name:"Gabarito",color:"#6ea8fe",values:errs.map(e=>e.orig_count)},
     {name:"Usuários",color:"#d2a8ff",values:errs.map(e=>e.user_count)}]);
  const ea=[...DATA.errors].filter(e=>(e.tp+e.fp+e.fn)>0)
    .sort((a,b)=>b.agreement-a.agreement);
  el("chartErrAgree").innerHTML=svgHBars(ea.map(e=>({label:e.nome,value:e.agreement*100,
    color:colorScale(e.agreement)})),{percent:true});
  el("chartMae").innerHTML=svgBars(U.map(u=>({label:userLabel(u.usuario),
    value:u.aval_mae==null?0:u.aval_mae,color:"#d2a8ff"})),{decimals:2});
  const ad=DATA.aval_distribution||{labels:[],original:[],users:[]};
  el("chartAval").innerHTML=svgGrouped(ad.labels.map(String),
    [{name:"Gabarito",color:"#6ea8fe",values:ad.original},
     {name:"Usuários",color:"#d2a8ff",values:ad.users}]);
}
function colorScale(t){ t=Math.max(0,Math.min(1,t));
  if(t<0.5)return "#ff7b72"; if(t<0.75)return "#ffb454"; return "#3ddc97"; }

/* ---------------- exports ---------------- */
async function doExport(method){
  busy(true);
  try{const r=await api(method);
    if(r.cancelled){busy(false);return;}
    if(r.ok)toast("Exportado: "+(r.path||r.folder),"good"); else toast(r.error||"falhou","bad");
  }catch(e){toast(e.message,"bad");}
  busy(false);
}
function svgToPng(svg,title){
  return new Promise((res)=>{
    try{
      const xml=new XMLSerializer().serializeToString(svg);
      const url="data:image/svg+xml;base64,"+btoa(unescape(encodeURIComponent(xml)));
      const img=new Image();
      img.onload=()=>{const sc=4;const w=(svg.viewBox&&svg.viewBox.baseVal.width)||svg.clientWidth||560;
        const h=(svg.viewBox&&svg.viewBox.baseVal.height)||svg.clientHeight||300;
        const c=document.createElement("canvas");c.width=w*sc;c.height=h*sc;
        const ctx=c.getContext("2d");ctx.fillStyle="#0f1420";ctx.fillRect(0,0,c.width,c.height);
        ctx.drawImage(img,0,0,c.width,c.height);
        try{res(c.toDataURL("image/png"));}catch(e){res(null);} };
      img.onerror=()=>res(null);
      img.src=url;
    }catch(e){res(null);}
  });
}
async function collectCharts(){
  const out=[]; const cards=document.querySelectorAll(".chart-card");
  for(const card of cards){const svg=card.querySelector("svg"); if(!svg)continue;
    const title=card.querySelector("h4").textContent;
    const data=await svgToPng(svg,title);
    if(data)out.push({name:card.dataset.chart,title:title,dataUrl:data});}
  return out;
}
async function prepareExportCharts(){
  if(activeTab!=="stats"){
    setTab("stats");
    await new Promise(r=>setTimeout(r,140));
  }else if(statsDirty&&DATA){
    renderStats(); statsDirty=false;
    await new Promise(r=>setTimeout(r,40));
  }
  return await collectCharts();
}
async function exportJson(){busy(true);
  try{
    const charts=await prepareExportCharts();
    const r=await api("export_json",charts,anonymizeUsers);
    if(r.cancelled){busy(false);return;}
    r.ok?toast("JSON salvo: "+r.path,"good"):toast(r.error||"falhou","bad");
  }catch(e){toast(e.message,"bad");}busy(false);}
async function exportPdf(){busy(true);
  try{
    const charts=await prepareExportCharts();
    const r=await api("export_pdf_report",charts,anonymizeUsers);
    if(r.cancelled){busy(false);return;}
    r.ok?toast("PDF salvo: "+r.path,"good"):toast(r.error||"falhou","bad");
  }catch(e){toast(e.message,"bad");}busy(false);}
async function exportPng(){busy(true);
  try{if(activeTab!=="stats"){setTab("stats");await new Promise(r=>setTimeout(r,120));}
    const charts=await collectCharts();
    if(!charts.length){toast("Nenhum gráfico para exportar","bad");busy(false);return;}
    const r=await api("export_charts",charts);
    if(r.cancelled){busy(false);return;}
    r.ok?toast("Gráficos salvos em "+r.folder,"good"):toast(r.error||"falhou","bad");
  }catch(e){toast(e.message,"bad");}busy(false);}
async function exportHtml(){busy(true);
  try{if(activeTab!=="stats"){setTab("stats");await new Promise(r=>setTimeout(r,120));}
    const charts=await collectCharts();
    const r=await api("export_html_report",charts);
    if(r.cancelled){busy(false);return;}
    r.ok?toast("Relatório salvo: "+r.path,"good"):toast(r.error||"falhou","bad");
  }catch(e){toast(e.message,"bad");}busy(false);}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    try:
        import webview
    except Exception:
        print("ERRO: pywebview nao instalado. Rode: pip install pywebview")
        sys.exit(1)

    def _mouse_screen_rect():
        """Retorna (x, y, w, h) da tela onde esta o mouse."""
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class POINT(ctypes.Structure):
                    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

                class RECT(ctypes.Structure):
                    _fields_ = [
                        ("left", wintypes.LONG),
                        ("top", wintypes.LONG),
                        ("right", wintypes.LONG),
                        ("bottom", wintypes.LONG),
                    ]

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", RECT),
                        ("rcWork", RECT),
                        ("dwFlags", wintypes.DWORD),
                    ]

                user32 = ctypes.windll.user32
                user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
                user32.GetCursorPos.restype = wintypes.BOOL
                user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
                user32.MonitorFromPoint.restype = wintypes.HMONITOR
                user32.GetMonitorInfoW.argtypes = [
                    wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
                user32.GetMonitorInfoW.restype = wintypes.BOOL

                pt = POINT()
                if not user32.GetCursorPos(ctypes.byref(pt)):
                    return None
                monitor = user32.MonitorFromPoint(
                    pt, 2)  # MONITOR_DEFAULTTONEAREST
                if not monitor:
                    return None
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    return None
                r = info.rcMonitor
                return (r.left, r.top, r.right - r.left, r.bottom - r.top)
            except Exception:
                return None

        try:
            from qtpy.QtGui import QCursor
            from qtpy.QtWidgets import QApplication

            app = QApplication.instance()
            if app:
                screen = app.screenAt(QCursor.pos()) or app.primaryScreen()
                if screen:
                    geo = screen.geometry()
                    return (geo.x(), geo.y(), geo.width(), geo.height())
        except Exception:
            pass

        return None

    api = Api()
    mouse_screen = _mouse_screen_rect()
    # A janela e criada "normal" e so depois (em _on_start) e colocada em tela
    # cheia. Criar ja com fullscreen=True + frameless e instavel no backend Qt
    # (as vezes abre como janela flutuante), por isso forcamos depois.
    #
    # IMPORTANTE (Linux/X11): resizable=True e proposital. Com resizable=False o
    # pywebview chama setFixedSize() no Qt, o que trava o tamanho maximo e impede
    # o showFullScreen() de cobrir a tela no Linux (a janela "vira fullscreen"
    # mas continua pequena). No Windows isso e ignorado. Como a janela e
    # frameless e easy_drag=False, o usuario nao consegue redimensionar nem
    # arrastar mesmo com resizable=True, entao na pratica continua "fixa".
    win_kwargs = dict(
        title="%s %s" % (APP_NAME, APP_VERSION),
        html=build_index_html(),
        js_api=api,
        frameless=True,
        resizable=True,
        easy_drag=False,
        background_color="#0f1420",
    )
    if mouse_screen:
        mx, my, mw, mh = mouse_screen
        win_kwargs.update(x=mx, y=my, width=min(1000, mw), height=min(700, mh))
    optional = dict(text_select=False)

    def _create(extra):
        kw = dict(win_kwargs)
        kw.update(extra)
        return webview.create_window(**kw)

    try:
        window = _create(optional)
    except TypeError:
        try:
            window = _create({})
        except TypeError:
            win_kwargs.pop("easy_drag", None)
            window = _create({})
    api.window = window

    def _on_window_exit():
        _force_process_exit(0)

    try:
        # No Qt, `closed` acontece tarde demais em alguns fechamentos: a janela
        # some, mas o event loop continua vivo no terminal. `closing` dispara
        # antes da destruicao nativa e mata o processo no caminho mais curto.
        window.events.closing += _on_window_exit
        window.events.closed += _on_window_exit
    except Exception:
        pass

    watchdog_started = {"ok": False}

    def _start_windows_window_watchdog():
        """Mata o processo se a janela sumir mas o loop GUI continuar vivo."""
        if sys.platform != "win32" or watchdog_started["ok"]:
            return
        watchdog_started["ok"] = True
        pid = os.getpid()
        title = win_kwargs["title"]

        def _has_app_window():
            try:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.windll.user32
                user32.GetWindowThreadProcessId.argtypes = [
                    wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
                user32.GetWindowThreadProcessId.restype = wintypes.DWORD
                user32.IsWindowVisible.argtypes = [wintypes.HWND]
                user32.IsWindowVisible.restype = wintypes.BOOL
                user32.IsIconic.argtypes = [wintypes.HWND]
                user32.IsIconic.restype = wintypes.BOOL
                user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
                user32.GetWindowTextLengthW.restype = ctypes.c_int
                user32.GetWindowTextW.argtypes = [
                    wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
                user32.GetWindowTextW.restype = ctypes.c_int

                found = {"ok": False}
                enum_proc = ctypes.WINFUNCTYPE(
                    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

                def callback(hwnd, _):
                    owner = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(
                        hwnd, ctypes.byref(owner))
                    if owner.value != pid:
                        return True
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length <= 0:
                        return True
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    text = buff.value or ""
                    if title not in text and APP_NAME not in text:
                        return True
                    if (user32.IsWindowVisible(hwnd) or
                            user32.IsIconic(hwnd)):
                        found["ok"] = True
                        return False
                    return True

                user32.EnumWindows(enum_proc(callback), 0)
                return found["ok"]
            except Exception:
                return True

        def _watch():
            missing_since = None
            while True:
                try:
                    time.sleep(0.5)
                    if _has_app_window():
                        missing_since = None
                        continue
                    now = time.monotonic()
                    if missing_since is None:
                        missing_since = now
                    elif now - missing_since >= 2.0:
                        _force_process_exit(0)
                except Exception:
                    missing_since = None

        threading.Thread(
            target=_watch, name="BenaExtWindowWatchdog", daemon=True).start()

    def _move_to_mouse_screen():
        """Move a janela para a tela atual do mouse antes do fullscreen."""
        rect = _mouse_screen_rect() or mouse_screen
        if not rect:
            return False
        x, y, w, h = rect
        try:
            window.move(x, y)
        except Exception:
            return False
        try:
            window.resize(min(1000, w), min(700, h))
        except Exception:
            pass
        return True

    def _fill_screen():
        """Cobre a tela inteira via geometria (fallback p/ frameless)."""
        try:
            rect = _mouse_screen_rect() or mouse_screen
            if rect:
                x, y, w, h = rect
                if w > 0 and h > 0:
                    window.move(x, y)
                    window.resize(w, h)
                    return True
            screens = getattr(webview, "screens", None) or []
            if screens:
                sc = screens[0]
                w = int(getattr(sc, "width", 0) or 0)
                h = int(getattr(sc, "height", 0) or 0)
                x = int(getattr(sc, "x", 0) or 0)
                y = int(getattr(sc, "y", 0) or 0)
                if w > 0 and h > 0:
                    window.move(x, y)
                    window.resize(w, h)
                    return True
        except Exception:
            pass
        return False

    def _qt_browser():
        """Retorna a janela Qt nativa quando o backend em uso for Qt."""
        try:
            native = getattr(window, "native", None)
            if native is not None:
                return native
            import webview.platforms.qt as qt_backend
            return qt_backend.BrowserView.instances.get(window.uid)
        except Exception:
            return None

    def _qt_force_fullscreen():
        """Forca fullscreen no Qt sem usar toggle, que pode ficar invertido."""
        try:
            browser = _qt_browser()
            if not browser:
                return False
            from qtpy import QtCore

            def invoke(method):
                try:
                    QtCore.QMetaObject.invokeMethod(
                        browser, method, QtCore.Qt.QueuedConnection)
                    return True
                except Exception:
                    try:
                        getattr(browser, method)()
                        return True
                    except Exception:
                        return False

            try:
                if browser.isMinimized():
                    invoke("showNormal")
            except Exception:
                pass
            ok = invoke("showFullScreen")
            try:
                browser.is_fullscreen = True
            except Exception:
                pass
            return ok
        except Exception:
            return False

    def _restore_fullscreen_after_minimize():
        if not FULLSCREEN_MODE:
            return True
        if _qt_force_fullscreen():
            return True
        if _fill_screen():
            return True
        try:
            window.maximize()
            return True
        except Exception:
            return False

    api.restore_fullscreen_callback = _restore_fullscreen_after_minimize
    try:
        def _on_window_restored():
            api.win_restore_fullscreen()
        window.events.restored += _on_window_restored
    except Exception:
        pass

    def _enter_fullscreen():
        """Coloca a janela em tela cheia (Qt nativo) com varios fallbacks."""
        _move_to_mouse_screen()
        if _qt_force_fullscreen():       # showFullScreen nativo (Qt)
            return True
        try:
            window.toggle_fullscreen()   # API do pywebview (cobre GTK tambem)
            return True
        except Exception:
            pass
        if _fill_screen():               # preenche a tela por geometria
            return True
        try:
            window.maximize()
            return True
        except Exception:
            return False

    def _on_start():
        # ATENCAO (timing): o pywebview inicia a thread deste callback ANTES de
        # criar e exibir a janela (ver webview/__init__.py: thread.start() vem
        # antes de guilib.create_window). Se forcarmos fullscreen agora, no
        # Linux/X11 o gerenciador de janelas ainda nao mapeou a janela e o
        # showFullScreen e ignorado -> o app abre pequeno. Por isso esperamos o
        # evento 'shown' e damos um respiro ao WM antes de cobrir a tela.
        try:
            window.events.shown.wait(8)
        except Exception:
            pass
        try:
            time.sleep(0.25)
        except Exception:
            pass
        _start_windows_window_watchdog()

        if not FULLSCREEN_MODE:
            try:
                window.maximize()
            except Exception:
                pass
            return

        _enter_fullscreen()
        # Segunda tentativa: alguns WMs do Linux so aceitam o fullscreen depois
        # que a janela ja esteve visivel por um instante. _qt_force_fullscreen
        # usa showFullScreen direto (idempotente), entao reasseguramos aqui.
        try:
            time.sleep(0.4)
        except Exception:
            pass
        if not _qt_force_fullscreen():
            _fill_screen()

    # tenta o backend preferido; se indisponivel, cai para auto-deteccao
    # e, no Windows, para Qt como ultimo recurso.
    backends = []
    qt_fallback = (
        "qt"
        if sys.platform == "win32" and not getattr(sys, "frozen", False)
        else None
    )
    for candidate in (GUI_BACKEND, None, qt_fallback):
        if candidate not in backends:
            backends.append(candidate)
    last_exc = None
    for gui in backends:
        try:
            if gui:
                if gui == "qt":
                    _install_qt_message_filter()
                webview.start(_on_start, debug=False, gui=gui)
            else:
                webview.start(_on_start, debug=False)
            return
        except Exception as exc:  # backend indisponivel
            last_exc = exc
            continue
    if last_exc:
        raise last_exc


if __name__ == "__main__":
    main()
