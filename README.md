# Bot de tarefas e avisos do bar (Telegram)

Um bot (@D7bar_bot) em dois grupos:

- **D7** (integrantes): recebe as tarefas com botão **✅ Feito**, a lista fixada no topo e os avisos.
- **D7 - Nois** (gerentes): cria tarefas, recebe "fulano marcou #N" e **Confirma** ou **Devolve**.

## Fluxo

1. Gerente manda `/tarefa Limpar a chopeira` no grupo dos gerentes (várias de uma vez: uma por linha).
2. Bot posta `#12 Limpar a chopeira` com botão ✅ Feito no grupo dos integrantes e atualiza a lista fixada.
3. Funcionário aperta ✅ → tarefa fica **⏳ aguardando verificação** (continua na lista). No grupo dos gerentes chega "João marcou #12" com **✅ Confirmar / ↩️ Devolver**.
4. Gerente aperta Confirmar → sai da lista. Devolver → volta como ⚠️ refazer e o João é avisado.

## Rodar

Variáveis de ambiente (Railway → Variables):

| variável | valor |
|---|---|
| `BOT_TOKEN` | token do @BotFather |
| `DATABASE_URL` | Postgres do Neon (`postgresql://...?sslmode=require`) |

Start command: `python bot.py` (Procfile já configurado). As tabelas são criadas sozinhas na primeira execução.

## Configurar os grupos (uma vez)

Com o bot no ar e como admin nos dois grupos:

- no grupo dos integrantes: `/aqui_integrantes`
- no grupo dos gerentes: `/aqui_gerentes`

## Comandos (grupo dos gerentes)

| comando | faz |
|---|---|
| `/tarefa texto` | cria tarefa (uma por linha para criar várias) |
| `/lista` | tarefas pendentes |
| `/confirmar 12` | confirma a #12 (ou botão) |
| `/devolver 12 motivo` | devolve a #12 para refazer |
| `/remover 12` | tira a #12 da lista |
| `/aviso texto` | manda aviso com botão 👍 Li (quem leu aparece na mensagem) |
| `/checklist` | itens e horários do checklist diário |
| `/checklist_add abertura texto` | adiciona item (várias linhas = vários itens) |
| `/checklist_rm 3` | remove item 3 |
| `/checklist_hora abertura 15:00` | horário de envio diário (abertura ou fechamento) |
| `/checklist_agora abertura` | dispara o checklist agora |
| `/status` | configuração atual |

No grupo dos integrantes só existe `/lista` e `/ajuda`.

## Checklist diário

Sai todo dia no horário configurado como **uma mensagem com um botão por item**. Cada item passa pela mesma verificação da gerência. Itens de uma rodada anterior que ficaram sem fazer são cancelados na rodada seguinte e listados no grupo dos gerentes.
