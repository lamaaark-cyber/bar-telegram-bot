"""
Bot de tarefas e avisos do bar (Telegram).

Dois grupos, um bot:
  - grupo dos integrantes: recebe as tarefas com botão ✅ Feito, a lista fixada e os avisos
  - grupo dos gerentes: cria tarefas, recebe "fulano marcou #N" e confirma/devolve

Variáveis de ambiente:
  BOT_TOKEN     token do @BotFather
  DATABASE_URL  Postgres (Neon)  ex.: postgresql://user:senha@host/db?sslmode=require
"""

import html
import logging
import os
import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bar-bot")

TZ = ZoneInfo("America/Sao_Paulo")
TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

TIPOS_CHECKLIST = {"abertura": "🌅", "fechamento": "🌙"}

pool: AsyncConnectionPool | None = None


# ───────────────────────── banco ─────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tarefas (
    id SERIAL PRIMARY KEY,
    texto TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pendente',      -- pendente | aguardando | concluida | cancelada
    origem TEXT NOT NULL DEFAULT 'manual',        -- manual | checklist
    checklist_tipo TEXT,                          -- abertura | fechamento
    criada_por TEXT,
    criada_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    marcada_por_id BIGINT,
    marcada_por_nome TEXT,
    marcada_em TIMESTAMPTZ,
    verificada_por TEXT,
    verificada_em TIMESTAMPTZ,
    devolucoes INT NOT NULL DEFAULT 0,
    motivo_devolucao TEXT,
    msg_integrantes_id BIGINT,
    msg_gerentes_id BIGINT
);
CREATE TABLE IF NOT EXISTS checklist_itens (
    id SERIAL PRIMARY KEY,
    tipo TEXT NOT NULL,
    texto TEXT NOT NULL,
    ordem INT NOT NULL DEFAULT 0,
    ativo BOOLEAN NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS avisos (
    id SERIAL PRIMARY KEY,
    texto TEXT NOT NULL,
    criado_por TEXT,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    msg_integrantes_id BIGINT
);
CREATE TABLE IF NOT EXISTS aviso_leituras (
    aviso_id INT REFERENCES avisos(id),
    user_id BIGINT,
    nome TEXT,
    lido_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (aviso_id, user_id)
);
"""


async def db_fetch(sql: str, *args):
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


async def db_one(sql: str, *args):
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchone()


async def db_exec(sql: str, *args):
    async with pool.connection() as conn:
        await conn.execute(sql, args)


async def cfg(chave: str, default=None):
    row = await db_one("SELECT valor FROM config WHERE chave = %s", chave)
    return row["valor"] if row else default


async def set_cfg(chave: str, valor):
    await db_exec(
        "INSERT INTO config (chave, valor) VALUES (%s, %s) "
        "ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor",
        chave, str(valor),
    )


async def chat_integrantes() -> int | None:
    v = await cfg("chat_integrantes")
    return int(v) if v else None


async def chat_gerentes() -> int | None:
    v = await cfg("chat_gerentes")
    return int(v) if v else None


async def tarefa(tid: int):
    return await db_one("SELECT * FROM tarefas WHERE id = %s", tid)


# ───────────────────────── utilidades ─────────────────────────

esc = html.escape


def agora() -> datetime:
    return datetime.now(TZ)


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(TZ).strftime("%d/%m %H:%M")


def fmt_hora(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(TZ).strftime("%H:%M")


def mention(user_id: int | None, nome: str) -> str:
    if not user_id:
        return f"<b>{esc(nome)}</b>"
    return f'<a href="tg://user?id={user_id}">{esc(nome)}</a>'


def limpar_linhas(texto: str) -> list[str]:
    """Quebra texto em itens, um por linha, removendo marcadores tipo '- ', '• ', '1. '."""
    itens = []
    for linha in texto.splitlines():
        linha = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", linha).strip()
        if linha:
            itens.append(linha)
    return itens


def texto_do_comando(update: Update) -> str:
    """Tudo que vem depois do /comando (inclusive linhas seguintes)."""
    partes = (update.message.text or "").split(maxsplit=1)
    return partes[1] if len(partes) > 1 else ""


async def editar(bot, chat_id, message_id, texto, markup=None):
    """Edita uma mensagem ignorando o erro 'message is not modified'."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=texto,
            reply_markup=markup, parse_mode=ParseMode.HTML,
        )
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return True
        log.warning("não consegui editar msg %s em %s: %s", message_id, chat_id, e)
        return False


async def responder(update: Update, texto: str):
    await update.message.reply_text(texto, parse_mode=ParseMode.HTML)


# ───────────────────────── renderização ─────────────────────────

def kb_feito(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Feito", callback_data=f"done:{tid}")]])


def kb_verificar(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmar", callback_data=f"ok:{tid}"),
        InlineKeyboardButton("↩️ Devolver", callback_data=f"back:{tid}"),
    ]])


def render_manual(t) -> tuple[str, InlineKeyboardMarkup | None]:
    """Mensagem de uma tarefa manual no grupo dos integrantes."""
    tid, texto, status = t["id"], esc(t["texto"]), t["status"]
    if status == "pendente":
        linhas = [f"📌 <b>#{tid}</b> {texto}"]
        if t["devolucoes"]:
            motivo = f": {esc(t['motivo_devolucao'])}" if t.get("motivo_devolucao") else ""
            linhas.append(f"⚠️ <b>Devolvida — precisa refazer</b>{motivo}")
        linhas.append(f"<i>por {esc(t['criada_por'] or '')} · {fmt_dt(t['criada_em'])}</i>")
        return "\n".join(linhas), kb_feito(tid)
    if status == "aguardando":
        return (
            f"⏳ <b>#{tid}</b> {texto}\n"
            f"Marcada como feita por <b>{esc(t['marcada_por_nome'] or '')}</b> às {fmt_hora(t['marcada_em'])} — "
            f"aguardando verificação",
            None,
        )
    if status == "concluida":
        return (
            f"✅ <s>#{tid} {texto}</s>\n"
            f"<i>feita por {esc(t['marcada_por_nome'] or '')}, confirmada por "
            f"{esc(t['verificada_por'] or '')} às {fmt_hora(t['verificada_em'])}</i>",
            None,
        )
    return f"❌ <s>#{tid} {texto}</s>\n<i>removida</i>", None


def render_checklist(tipo: str, itens: list, data: datetime) -> tuple[str, InlineKeyboardMarkup | None]:
    """Mensagem única do checklist (um botão por item) no grupo dos integrantes."""
    emoji = TIPOS_CHECKLIST.get(tipo, "📋")
    titulo = f"{emoji} <b>CHECKLIST DE {tipo.upper()}</b> — {data.astimezone(TZ).strftime('%d/%m')}"
    ativos = [i for i in itens if i["status"] != "cancelada"]
    feitos = [i for i in ativos if i["status"] == "concluida"]
    if ativos and len(feitos) == len(ativos):
        corpo = "\n".join(f"✅ <s>{esc(i['texto'])}</s>" for i in ativos)
        return f"{titulo}\n<b>Completo!</b> 🎉\n\n{corpo}", None
    if not ativos:
        return f"{titulo}\n<i>cancelado</i>", None

    linhas = []
    for i in ativos:
        if i["status"] == "pendente":
            label = f"☐ {i['texto']}"
            if i["devolucoes"]:
                label = f"☐ ⚠️ {i['texto']} (refazer)"
            linhas.append([InlineKeyboardButton(label[:60], callback_data=f"done:{i['id']}")])
        elif i["status"] == "aguardando":
            label = f"⏳ {i['texto']} · {i['marcada_por_nome'] or ''}"
            linhas.append([InlineKeyboardButton(label[:60], callback_data="noop")])
        else:
            linhas.append([InlineKeyboardButton(f"✅ {i['texto']}"[:60], callback_data="noop")])
    texto = (
        f"{titulo}\n"
        f"Toca no item quando terminar. {len(feitos)}/{len(ativos)} confirmados."
    )
    return texto, InlineKeyboardMarkup(linhas)


def render_lista(tarefas: list, quando: datetime) -> str:
    """Texto da lista fixada."""
    tarefas = [t for t in tarefas if t["status"] in ("pendente", "aguardando")]
    manuais = [t for t in tarefas if t["origem"] == "manual"]
    checks = [t for t in tarefas if t["origem"] == "checklist"]
    out = [f"📋 <b>TAREFAS DO BAR</b>  <i>({quando.strftime('%d/%m %H:%M')})</i>"]

    if not tarefas:
        out.append("\n🎉 Nenhuma tarefa pendente.")
        return "\n".join(out)

    pend = [t for t in manuais if t["status"] == "pendente"]
    aguard = [t for t in manuais if t["status"] == "aguardando"]
    if pend:
        out.append("\n<b>A fazer</b>")
        for t in pend:
            flag = "  ⚠️ refazer" if t["devolucoes"] else ""
            out.append(f"#{t['id']} {esc(t['texto'])}{flag}")
    if aguard:
        out.append("\n<b>Aguardando verificação</b>")
        for t in aguard:
            out.append(f"#{t['id']} {esc(t['texto'])} — ⏳ {esc(t['marcada_por_nome'] or '')} {fmt_hora(t['marcada_em'])}")

    for tipo, emoji in TIPOS_CHECKLIST.items():
        do_tipo = [t for t in checks if t["checklist_tipo"] == tipo]
        if not do_tipo:
            continue
        out.append(f"\n{emoji} <b>Checklist de {tipo}</b>")
        for t in do_tipo:
            if t["status"] == "pendente":
                flag = " ⚠️" if t["devolucoes"] else ""
                out.append(f"☐ {esc(t['texto'])}{flag}")
            else:
                out.append(f"⏳ {esc(t['texto'])} ({esc(t['marcada_por_nome'] or '')})")

    out.append("\n<i>Marcou ✅ na tarefa → a gerência confere → sai da lista.</i>")
    return "\n".join(out)


# ───────────────────────── lista fixada ─────────────────────────

async def atualizar_lista(context: ContextTypes.DEFAULT_TYPE):
    chat = await chat_integrantes()
    if not chat:
        return
    tarefas = await db_fetch(
        "SELECT * FROM tarefas WHERE status IN ('pendente','aguardando') ORDER BY id"
    )
    texto = render_lista(tarefas, agora())
    bot = context.bot
    msg_id = await cfg("lista_msg_id")
    if msg_id and await editar(bot, chat, int(msg_id), texto):
        return
    # não existe ou foi apagada: cria e fixa de novo
    m = await bot.send_message(chat, texto, parse_mode=ParseMode.HTML, disable_notification=True)
    await set_cfg("lista_msg_id", m.message_id)
    try:
        await bot.pin_chat_message(chat, m.message_id, disable_notification=True)
    except TelegramError as e:
        log.warning("não consegui fixar a lista: %s", e)


async def refresh_msg_tarefa(context: ContextTypes.DEFAULT_TYPE, t):
    """Reedita a mensagem da tarefa (ou do checklist inteiro) no grupo dos integrantes."""
    chat = await chat_integrantes()
    if not chat or not t["msg_integrantes_id"]:
        return
    if t["origem"] == "checklist":
        itens = await db_fetch(
            "SELECT * FROM tarefas WHERE msg_integrantes_id = %s AND origem = 'checklist' ORDER BY id",
            t["msg_integrantes_id"],
        )
        texto, markup = render_checklist(t["checklist_tipo"], itens, t["criada_em"])
    else:
        texto, markup = render_manual(t)
    await editar(context.bot, chat, t["msg_integrantes_id"], texto, markup)


# ───────────────────────── guardas ─────────────────────────

async def eh_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("creator", "administrator")
    except TelegramError:
        return False


async def so_gerentes(update: Update) -> bool:
    ger = await chat_gerentes()
    if not ger:
        await responder(update, "Grupo dos gerentes ainda não configurado. Manda <code>/aqui_gerentes</code> lá dentro.")
        return False
    if update.effective_chat.id != ger:
        await responder(update, "Esse comando só funciona no grupo dos gerentes.")
        return False
    return True


# ───────────────────────── comandos: configuração ─────────────────────────

async def cmd_aqui(update: Update, context: ContextTypes.DEFAULT_TYPE, papel: str):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await responder(update, "Manda esse comando dentro do grupo.")
        return
    if not await eh_admin(context.bot, chat.id, update.effective_user.id):
        await responder(update, "Só administrador do grupo pode fazer isso.")
        return
    await set_cfg(f"chat_{papel}", chat.id)
    await responder(update, f"✅ Este grupo agora é o dos <b>{papel}</b>.")
    await registrar_comandos(context.application)
    if papel == "integrantes":
        await atualizar_lista(context)


async def cmd_aqui_integrantes(update, context):
    await cmd_aqui(update, context, "integrantes")


async def cmd_aqui_gerentes(update, context):
    await cmd_aqui(update, context, "gerentes")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    linhas = [
        f"integrantes: <code>{await cfg('chat_integrantes', '—')}</code>",
        f"gerentes: <code>{await cfg('chat_gerentes', '—')}</code>",
        f"lista fixada: msg <code>{await cfg('lista_msg_id', '—')}</code>",
        f"checklist abertura: {await cfg('hora_abertura', 'sem horário')}",
        f"checklist fechamento: {await cfg('hora_fechamento', 'sem horário')}",
        f"jobs: {', '.join(j.name for j in context.job_queue.jobs()) or 'nenhum'}",
    ]
    await responder(update, "\n".join(linhas))


async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ger = await chat_gerentes()
    if ger and update.effective_chat.id == ger:
        txt = (
            "<b>Tarefas</b>\n"
            "/tarefa texto — cria tarefa (uma por linha pra criar várias)\n"
            "/lista — tarefas pendentes\n"
            "/confirmar 12 — confirma a #12 (ou usa o botão)\n"
            "/devolver 12 motivo — devolve a #12 pra refazer\n"
            "/remover 12 — tira a #12 da lista\n\n"
            "<b>Avisos</b>\n"
            "/aviso texto — manda aviso com botão 👍 Li\n\n"
            "<b>Checklist diário</b>\n"
            "/checklist — ver itens e horários\n"
            "/checklist_add abertura texto — adiciona item (várias linhas = vários itens)\n"
            "/checklist_rm 3 — remove item 3\n"
            "/checklist_hora abertura 15:00 — horário de envio\n"
            "/checklist_agora abertura — dispara agora\n\n"
            "/status — configuração atual"
        )
    else:
        txt = (
            "Quando terminar uma tarefa, toca em <b>✅ Feito</b> na mensagem dela. "
            "A gerência confere e ela sai da lista fixada no topo.\n\n"
            "/lista — ver o que está pendente"
        )
    await responder(update, txt)


# ───────────────────────── comandos: tarefas ─────────────────────────

async def postar_tarefa(context: ContextTypes.DEFAULT_TYPE, t):
    chat = await chat_integrantes()
    texto, markup = render_manual(t)
    m = await context.bot.send_message(chat, texto, parse_mode=ParseMode.HTML, reply_markup=markup)
    await db_exec("UPDATE tarefas SET msg_integrantes_id = %s WHERE id = %s", m.message_id, t["id"])


async def cmd_tarefa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    if not await chat_integrantes():
        await responder(update, "Grupo dos integrantes ainda não configurado. Manda <code>/aqui_integrantes</code> lá.")
        return
    itens = limpar_linhas(texto_do_comando(update))
    if not itens:
        await responder(update, "Uso: <code>/tarefa Limpar a chopeira</code>\nVárias de uma vez: uma por linha.")
        return
    autor = update.effective_user.full_name
    ids = []
    for texto in itens:
        row = await db_one(
            "INSERT INTO tarefas (texto, criada_por) VALUES (%s, %s) RETURNING *", texto, autor
        )
        await postar_tarefa(context, row)
        ids.append(row["id"])
    await atualizar_lista(context)
    lista = ", ".join(f"#{i}" for i in ids)
    await responder(update, f"✅ Criada{'s' if len(ids) > 1 else ''}: {lista}")


async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tarefas = await db_fetch(
        "SELECT * FROM tarefas WHERE status IN ('pendente','aguardando') ORDER BY id"
    )
    await responder(update, render_lista(tarefas, agora()))


def _id_dos_args(context) -> int | None:
    if context.args and context.args[0].lstrip("#").isdigit():
        return int(context.args[0].lstrip("#"))
    return None


async def cmd_remover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    tid = _id_dos_args(context)
    t = await tarefa(tid) if tid else None
    if not t:
        await responder(update, "Uso: <code>/remover 12</code>")
        return
    if t["status"] in ("concluida", "cancelada"):
        await responder(update, f"#{tid} já está {t['status']}.")
        return
    await db_exec("UPDATE tarefas SET status = 'cancelada' WHERE id = %s", tid)
    await refresh_msg_tarefa(context, await tarefa(tid))
    await atualizar_lista(context)
    await responder(update, f"❌ #{tid} removida.")


# ───────────────────────── verificação (confirmar / devolver) ─────────────────────────

async def confirmar(context: ContextTypes.DEFAULT_TYPE, t, quem: str) -> str:
    await db_exec(
        "UPDATE tarefas SET status = 'concluida', verificada_por = %s, verificada_em = now() WHERE id = %s",
        quem, t["id"],
    )
    t = await tarefa(t["id"])
    await refresh_msg_tarefa(context, t)
    await atualizar_lista(context)
    return f"✅ <b>#{t['id']} {esc(t['texto'])}</b> — feita por {esc(t['marcada_por_nome'] or '')}, confirmada por {esc(quem)} ({fmt_hora(t['verificada_em'])})"


async def devolver(context: ContextTypes.DEFAULT_TYPE, t, quem: str, motivo: str | None) -> str:
    await db_exec(
        "UPDATE tarefas SET status = 'pendente', devolucoes = devolucoes + 1, motivo_devolucao = %s, "
        "verificada_por = %s, verificada_em = now() WHERE id = %s",
        motivo, quem, t["id"],
    )
    t = await tarefa(t["id"])
    await refresh_msg_tarefa(context, t)
    await atualizar_lista(context)
    chat = await chat_integrantes()
    if chat:
        msg = (
            f"↩️ {mention(t['marcada_por_id'], t['marcada_por_nome'] or 'Equipe')}, a tarefa "
            f"<b>#{t['id']} {esc(t['texto'])}</b> foi devolvida — precisa refazer."
        )
        if motivo:
            msg += f"\nMotivo: {esc(motivo)}"
        await context.bot.send_message(chat, msg, parse_mode=ParseMode.HTML)
    extra = f"\nMotivo: {esc(motivo)}" if motivo else ""
    return f"↩️ <b>#{t['id']} {esc(t['texto'])}</b> — devolvida por {esc(quem)} ({fmt_hora(t['verificada_em'])}){extra}"


async def cmd_confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    tid = _id_dos_args(context)
    t = await tarefa(tid) if tid else None
    if not t:
        await responder(update, "Uso: <code>/confirmar 12</code>")
        return
    if t["status"] != "aguardando":
        await responder(update, f"#{tid} não está aguardando verificação (status: {t['status']}).")
        return
    txt = await confirmar(context, t, update.effective_user.full_name)
    if t["msg_gerentes_id"]:
        await editar(context.bot, update.effective_chat.id, t["msg_gerentes_id"], txt)
    await responder(update, txt)


async def cmd_devolver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    tid = _id_dos_args(context)
    t = await tarefa(tid) if tid else None
    if not t:
        await responder(update, "Uso: <code>/devolver 12 motivo opcional</code>")
        return
    if t["status"] != "aguardando":
        await responder(update, f"#{tid} não está aguardando verificação (status: {t['status']}).")
        return
    motivo = " ".join(context.args[1:]).strip() or None
    txt = await devolver(context, t, update.effective_user.full_name, motivo)
    if t["msg_gerentes_id"]:
        await editar(context.bot, update.effective_chat.id, t["msg_gerentes_id"], txt)
    await responder(update, txt)


# ───────────────────────── botões ─────────────────────────

async def cb_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tid = int(q.data.split(":")[1])
    t = await tarefa(tid)
    if not t or t["status"] != "pendente":
        await q.answer("Essa tarefa já foi marcada.", show_alert=False)
        if t:
            await refresh_msg_tarefa(context, t)
        return
    user = q.from_user
    await db_exec(
        "UPDATE tarefas SET status = 'aguardando', marcada_por_id = %s, marcada_por_nome = %s, "
        "marcada_em = now() WHERE id = %s AND status = 'pendente'",
        user.id, user.full_name, tid,
    )
    t = await tarefa(tid)
    await q.answer("Marcada! A gerência vai conferir. ✅")
    await refresh_msg_tarefa(context, t)
    await atualizar_lista(context)

    ger = await chat_gerentes()
    if ger:
        origem = f" <i>(checklist de {t['checklist_tipo']})</i>" if t["origem"] == "checklist" else ""
        txt = (
            f"⏳ {mention(user.id, user.full_name)} marcou <b>#{tid} {esc(t['texto'])}</b> como feita{origem} "
            f"às {fmt_hora(t['marcada_em'])}.\nConfere e decide:"
        )
        m = await context.bot.send_message(ger, txt, parse_mode=ParseMode.HTML, reply_markup=kb_verificar(tid))
        await db_exec("UPDATE tarefas SET msg_gerentes_id = %s WHERE id = %s", m.message_id, tid)


async def cb_verificar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    acao, tid = q.data.split(":")
    tid = int(tid)
    ger = await chat_gerentes()
    if q.message.chat.id != ger:
        await q.answer("Só no grupo dos gerentes.")
        return
    t = await tarefa(tid)
    if not t or t["status"] != "aguardando":
        await q.answer(f"#{tid} não está aguardando verificação.")
        return
    quem = q.from_user.full_name
    txt = await confirmar(context, t, quem) if acao == "ok" else await devolver(context, t, quem, None)
    await q.answer("Confirmada ✅" if acao == "ok" else "Devolvida ↩️")
    await editar(context.bot, ger, q.message.message_id, txt)


async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ───────────────────────── avisos ─────────────────────────

def render_aviso(a, leitores: list) -> tuple[str, InlineKeyboardMarkup]:
    txt = f"📢 <b>AVISO</b>\n\n{esc(a['texto'])}\n\n<i>— {esc(a['criado_por'] or '')}, {fmt_dt(a['criado_em'])}</i>"
    if leitores:
        nomes = ", ".join(esc(l["nome"]) for l in leitores)
        txt += f"\n\n👍 Lido por ({len(leitores)}): {nomes}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("👍 Li", callback_data=f"li:{a['id']}")]])
    return txt, kb


async def cmd_aviso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    chat = await chat_integrantes()
    if not chat:
        await responder(update, "Grupo dos integrantes ainda não configurado.")
        return
    texto = texto_do_comando(update).strip()
    if not texto:
        await responder(update, "Uso: <code>/aviso Sábado abrimos às 14h</code>")
        return
    a = await db_one(
        "INSERT INTO avisos (texto, criado_por) VALUES (%s, %s) RETURNING *",
        texto, update.effective_user.full_name,
    )
    txt, kb = render_aviso(a, [])
    m = await context.bot.send_message(chat, txt, parse_mode=ParseMode.HTML, reply_markup=kb)
    await db_exec("UPDATE avisos SET msg_integrantes_id = %s WHERE id = %s", m.message_id, a["id"])
    await responder(update, f"📢 Aviso #{a['id']} enviado. Quem apertar 👍 Li aparece na própria mensagem.")


async def cb_li(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    aid = int(q.data.split(":")[1])
    a = await db_one("SELECT * FROM avisos WHERE id = %s", aid)
    if not a:
        await q.answer()
        return
    await db_exec(
        "INSERT INTO aviso_leituras (aviso_id, user_id, nome) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        aid, q.from_user.id, q.from_user.full_name,
    )
    leitores = await db_fetch(
        "SELECT nome FROM aviso_leituras WHERE aviso_id = %s ORDER BY lido_em", aid
    )
    await q.answer("Registrado 👍")
    txt, kb = render_aviso(a, leitores)
    await editar(context.bot, q.message.chat.id, q.message.message_id, txt, kb)


# ───────────────────────── checklist ─────────────────────────

def _tipo_dos_args(context) -> str | None:
    if context.args and context.args[0].lower() in TIPOS_CHECKLIST:
        return context.args[0].lower()
    return None


async def cmd_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    itens = await db_fetch("SELECT * FROM checklist_itens WHERE ativo ORDER BY tipo, ordem, id")
    out = []
    for tipo, emoji in TIPOS_CHECKLIST.items():
        hora = await cfg(f"hora_{tipo}")
        out.append(f"{emoji} <b>{tipo.capitalize()}</b> — {hora or 'sem horário (use /checklist_hora)'}")
        do_tipo = [i for i in itens if i["tipo"] == tipo]
        if do_tipo:
            out += [f"  {i['id']}. {esc(i['texto'])}" for i in do_tipo]
        else:
            out.append("  <i>(nenhum item — use /checklist_add)</i>")
        out.append("")
    await responder(update, "\n".join(out).strip())


async def cmd_checklist_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    partes = (update.message.text or "").split(maxsplit=2)
    tipo = partes[1].lower() if len(partes) > 1 else ""
    if tipo not in TIPOS_CHECKLIST or len(partes) < 3:
        await responder(update, "Uso: <code>/checklist_add abertura Ligar a chopeira</code>\n(abertura ou fechamento; várias linhas = vários itens)")
        return
    itens = limpar_linhas(partes[2])
    row = await db_one("SELECT COALESCE(MAX(ordem), 0) AS m FROM checklist_itens WHERE tipo = %s", tipo)
    ordem = row["m"]
    for texto in itens:
        ordem += 1
        await db_exec("INSERT INTO checklist_itens (tipo, texto, ordem) VALUES (%s, %s, %s)", tipo, texto, ordem)
    await responder(update, f"✅ {len(itens)} item(ns) adicionado(s) ao checklist de {tipo}.")


async def cmd_checklist_rm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    iid = _id_dos_args(context)
    row = await db_one("SELECT * FROM checklist_itens WHERE id = %s AND ativo", iid) if iid else None
    if not row:
        await responder(update, "Uso: <code>/checklist_rm 3</code> (o número que aparece em /checklist)")
        return
    await db_exec("UPDATE checklist_itens SET ativo = false WHERE id = %s", iid)
    await responder(update, f"❌ Item {iid} ({esc(row['texto'])}) removido do checklist de {row['tipo']}.")


async def cmd_checklist_hora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    tipo = _tipo_dos_args(context)
    hora = context.args[1] if len(context.args) > 1 else ""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hora)
    if not tipo or not m or not (0 <= int(m[1]) < 24 and 0 <= int(m[2]) < 60):
        await responder(update, "Uso: <code>/checklist_hora abertura 15:00</code>")
        return
    hora = f"{int(m[1]):02d}:{int(m[2]):02d}"
    await set_cfg(f"hora_{tipo}", hora)
    await agendar_checklists(context.application)
    await responder(update, f"⏰ Checklist de {tipo} vai sair todo dia às {hora}.")


async def cmd_checklist_agora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await so_gerentes(update):
        return
    tipo = _tipo_dos_args(context)
    if not tipo:
        await responder(update, "Uso: <code>/checklist_agora abertura</code>")
        return
    n = await montar_checklist(context, tipo)
    await responder(update, f"Checklist de {tipo} enviado ({n} itens)." if n else f"Checklist de {tipo} está vazio.")


async def montar_checklist(context: ContextTypes.DEFAULT_TYPE, tipo: str) -> int:
    """Cria as tarefas do checklist e posta a mensagem única no grupo dos integrantes."""
    chat = await chat_integrantes()
    ger = await chat_gerentes()
    if not chat:
        return 0
    itens = await db_fetch(
        "SELECT * FROM checklist_itens WHERE tipo = %s AND ativo ORDER BY ordem, id", tipo
    )
    if not itens:
        return 0

    # itens de rodadas anteriores que ficaram pendentes: cancela e avisa a gerência
    velhos = await db_fetch(
        "SELECT * FROM tarefas WHERE origem = 'checklist' AND checklist_tipo = %s AND status = 'pendente'",
        tipo,
    )
    if velhos:
        await db_exec(
            "UPDATE tarefas SET status = 'cancelada' WHERE origem = 'checklist' AND checklist_tipo = %s AND status = 'pendente'",
            tipo,
        )
        for msg_id in {v["msg_integrantes_id"] for v in velhos if v["msg_integrantes_id"]}:
            amostra = await db_one("SELECT * FROM tarefas WHERE msg_integrantes_id = %s LIMIT 1", msg_id)
            if amostra:
                await refresh_msg_tarefa(context, amostra)
        if ger:
            lista = "\n".join(f"• {esc(v['texto'])}" for v in velhos)
            await context.bot.send_message(
                ger,
                f"⚠️ Itens do checklist de {tipo} anterior que <b>não foram feitos</b>:\n{lista}",
                parse_mode=ParseMode.HTML,
            )

    novos = []
    for i in itens:
        row = await db_one(
            "INSERT INTO tarefas (texto, origem, checklist_tipo, criada_por) VALUES (%s, 'checklist', %s, 'checklist') RETURNING *",
            i["texto"], tipo,
        )
        novos.append(row)
    texto, markup = render_checklist(tipo, novos, agora())
    m = await context.bot.send_message(chat, texto, parse_mode=ParseMode.HTML, reply_markup=markup)
    await db_exec(
        "UPDATE tarefas SET msg_integrantes_id = %s WHERE id = ANY(%s)",
        m.message_id, [r["id"] for r in novos],
    )
    await atualizar_lista(context)
    if ger:
        await context.bot.send_message(
            ger, f"{TIPOS_CHECKLIST[tipo]} Checklist de {tipo} enviado ({len(novos)} itens).",
            disable_notification=True,
        )
    return len(novos)


async def job_checklist(context: ContextTypes.DEFAULT_TYPE):
    await montar_checklist(context, context.job.data)


async def agendar_checklists(app: Application):
    for tipo in TIPOS_CHECKLIST:
        nome = f"checklist_{tipo}"
        for j in app.job_queue.get_jobs_by_name(nome):
            j.schedule_removal()
        hora = await cfg(f"hora_{tipo}")
        if not hora:
            continue
        h, m = (int(x) for x in hora.split(":"))
        app.job_queue.run_daily(job_checklist, time=time(h, m, tzinfo=TZ), data=tipo, name=nome)
        log.info("checklist de %s agendado para %s", tipo, hora)


# ───────────────────────── migração de grupo ─────────────────────────

async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grupo comum virou supergrupo: o chat_id muda, atualiza a config."""
    msg = update.message
    if msg and msg.migrate_from_chat_id:
        velho, novo = str(msg.migrate_from_chat_id), str(msg.chat.id)
        await db_exec("UPDATE config SET valor = %s WHERE chave IN ('chat_integrantes','chat_gerentes') AND valor = %s", novo, velho)
        log.info("grupo migrou %s -> %s", velho, novo)
        await registrar_comandos(context.application)


# ───────────────────────── menu de comandos ─────────────────────────

CMDS_PADRAO = [
    BotCommand("aqui_integrantes", "Marcar este grupo como o dos integrantes"),
    BotCommand("aqui_gerentes", "Marcar este grupo como o dos gerentes"),
    BotCommand("lista", "Ver tarefas pendentes"),
    BotCommand("ajuda", "Como funciona"),
]
CMDS_INTEGRANTES = [
    BotCommand("lista", "Ver tarefas pendentes"),
    BotCommand("ajuda", "Como funciona"),
]
CMDS_GERENTES = [
    BotCommand("tarefa", "Criar tarefa (uma por linha pra várias)"),
    BotCommand("lista", "Ver tarefas pendentes"),
    BotCommand("confirmar", "Confirmar: /confirmar 12"),
    BotCommand("devolver", "Devolver: /devolver 12 motivo"),
    BotCommand("remover", "Remover: /remover 12"),
    BotCommand("aviso", "Mandar aviso com botão Li"),
    BotCommand("checklist", "Itens e horários do checklist"),
    BotCommand("checklist_add", "/checklist_add abertura texto"),
    BotCommand("checklist_rm", "/checklist_rm 3"),
    BotCommand("checklist_hora", "/checklist_hora abertura 15:00"),
    BotCommand("checklist_agora", "Disparar checklist agora"),
    BotCommand("status", "Configuração atual"),
    BotCommand("ajuda", "Lista de comandos"),
]


async def registrar_comandos(app: Application):
    bot = app.bot
    try:
        await bot.set_my_commands(CMDS_PADRAO, scope=BotCommandScopeDefault())
        if (c := await chat_integrantes()):
            await bot.set_my_commands(CMDS_INTEGRANTES, scope=BotCommandScopeChat(c))
        if (c := await chat_gerentes()):
            await bot.set_my_commands(CMDS_GERENTES, scope=BotCommandScopeChat(c))
    except TelegramError as e:
        log.warning("não consegui registrar o menu de comandos: %s", e)


# ───────────────────────── ciclo de vida ─────────────────────────

async def post_init(app: Application):
    global pool
    pool = AsyncConnectionPool(
        conninfo=DATABASE_URL, min_size=1, max_size=4, open=False,
        kwargs={"row_factory": dict_row, "autocommit": True},
        check=AsyncConnectionPool.check_connection,
    )
    await pool.open()
    await db_exec(SCHEMA)
    await registrar_comandos(app)
    await agendar_checklists(app)
    log.info("bot no ar — integrantes=%s gerentes=%s", await cfg("chat_integrantes"), await cfg("chat_gerentes"))


async def post_shutdown(app: Application):
    if pool:
        await pool.close()


def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    grupos = filters.ChatType.GROUPS

    app.add_handler(CommandHandler("aqui_integrantes", cmd_aqui_integrantes, grupos))
    app.add_handler(CommandHandler("aqui_gerentes", cmd_aqui_gerentes, grupos))
    app.add_handler(CommandHandler("status", cmd_status, grupos))
    app.add_handler(CommandHandler(["ajuda", "start", "help"], cmd_ajuda))

    app.add_handler(CommandHandler("tarefa", cmd_tarefa, grupos))
    app.add_handler(CommandHandler("lista", cmd_lista))
    app.add_handler(CommandHandler("confirmar", cmd_confirmar, grupos))
    app.add_handler(CommandHandler("devolver", cmd_devolver, grupos))
    app.add_handler(CommandHandler("remover", cmd_remover, grupos))
    app.add_handler(CommandHandler("aviso", cmd_aviso, grupos))

    app.add_handler(CommandHandler("checklist", cmd_checklist, grupos))
    app.add_handler(CommandHandler("checklist_add", cmd_checklist_add, grupos))
    app.add_handler(CommandHandler("checklist_rm", cmd_checklist_rm, grupos))
    app.add_handler(CommandHandler("checklist_hora", cmd_checklist_hora, grupos))
    app.add_handler(CommandHandler("checklist_agora", cmd_checklist_agora, grupos))

    app.add_handler(CallbackQueryHandler(cb_done, pattern=r"^done:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_verificar, pattern=r"^(ok|back):\d+$"))
    app.add_handler(CallbackQueryHandler(cb_li, pattern=r"^li:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))

    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
