#!/usr/bin/env python3
"""
Monitor de estoque CFMOTO IBEX 450 - concessionarias de Piracicaba e Sao Paulo.

Le o HTML publico da pagina do produto (sem login) e observa dois sinais que o
proprio site entrega server-side:

  has_stock      -> false enquanto nao ha unidade; true quando o lote abre
  paywallActive  -> true quando a sala de espera virtual e ligada, o que na
                    pratica significa que um lote esta comecando AGORA

Alerta e so isso: avisa voce. Nao faz login, nao compra, nao preenche nada.
Rode na SUA maquina, no seu IP.

Uso:
    python3 monitor_cfmoto.py                # intervalo padrao de 5 min
    python3 monitor_cfmoto.py --intervalo 180
    python3 monitor_cfmoto.py --teste        # simula estoque para testar o alerta

Telegram (opcional, para receber no celular):
    export TELEGRAM_BOT_TOKEN="123456:ABC..."
    export TELEGRAM_CHAT_ID="987654321"
"""

import argparse
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

ALVOS = [
    ("Piracicaba", "https://reserva.cfmoto.com.br/produto/motorcycles-ibex-450/cfmoto-virage-piracicaba"),
    ("Sao Paulo",  "https://reserva.cfmoto.com.br/produto/motorcycles-ibex-450/cfmoto-c10-sao-paulo"),
]

# Intervalo minimo em segundos. Bater mais forte que isso no site nao te da
# vantagem nenhuma e e a forma mais rapida de tomar bloqueio de IP.
INTERVALO_MINIMO = 120
INTERVALO_PADRAO = 300

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

# O HTML vem com as aspas escapadas (payload do Next.js), entao aceitamos as
# duas formas: \"has_stock\":true  e  "has_stock":true
RE_STOCK = re.compile(r'\\?"has_stock\\?":\s*(true|false)')
RE_PAYWALL = re.compile(r'\\?"paywallActive\\?":\s*(true|false)')
# cada cor: slug + name, e o has_stock que vem logo depois
RE_COR = re.compile(r'\\?"slug\\?":\\?"([a-z0-9-]+)\\?",\\?"name\\?":\\?"([^"\\]{1,40})\\?",\\?"thumbnail_url')
RE_SINAL = re.compile(r'\\?"signalAmount\\?":\s*(\d+)')


def agora():
    return datetime.now().strftime("%d/%m %H:%M:%S")


def baixar(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def analisar(html):
    """Devolve dict: cores nomeadas com estoque, flag geral, paywall, sinal."""
    todos = [m == "true" for m in RE_STOCK.findall(html)]

    # casa cada cor com o has_stock que aparece logo apos ela
    cores = []
    for m in RE_COR.finditer(html):
        prox = RE_STOCK.search(html, m.end())
        if prox:
            cores.append((m.group(2), prox.group(1) == "true"))

    pw = RE_PAYWALL.search(html)
    sinal = RE_SINAL.search(html)
    return {
        "cores": cores,
        "com_estoque": [n for n, tem in cores if tem],
        "tem": any(todos),
        "paywall": bool(pw and pw.group(1) == "true"),
        "sinal": int(sinal.group(1)) if sinal else None,
        "n_campos": len(todos),
    }


def telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        dados = json.dumps({"chat_id": chat, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=dados, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"  [aviso] Telegram falhou: {e}")


def notificar_desktop(titulo, msg):
    so = platform.system()
    try:
        if so == "Darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{msg}" with title "{titulo}" sound name "Submarine"'],
                           check=False, timeout=10)
        elif so == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", titulo, msg],
                           check=False, timeout=10)
        elif so == "Windows":
            ps = (f'[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms");'
                  f'[System.Windows.Forms.MessageBox]::Show("{msg}","{titulo}")')
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           check=False, timeout=20)
    except Exception:
        pass


def alertar(local, url, detalhe):
    linha = "=" * 68
    msg = f"CFMOTO IBEX 450 LIBEROU em {local}! {detalhe}\n{url}"
    print(f"\n{linha}\n*** {msg} ***\n{linha}\n")
    for _ in range(12):          # sino do terminal, insistente
        sys.stdout.write("\a")
        sys.stdout.flush()
        time.sleep(0.35)
    notificar_desktop("CFMOTO IBEX 450 liberou!", f"{local} - {detalhe}")
    telegram(msg)


ARQ_ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cfmoto_estado.json")
ARQ_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cfmoto_historico.csv")


def carregar_estado():
    try:
        with open(ARQ_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def salvar_estado(estado):
    try:
        with open(ARQ_ESTADO, "w", encoding="utf-8") as f:
            json.dump(estado, f)
    except Exception:
        pass


def registrar(local, info):
    """Grava toda checagem em CSV. E assim que se descobre o padrao dos
    lotes pequenos: com que frequencia saem, em que horario, e por quanto
    tempo ficam no ar."""
    novo_arquivo = not os.path.exists(ARQ_LOG)
    try:
        with open(ARQ_LOG, "a", encoding="utf-8") as f:
            if novo_arquivo:
                f.write("datahora,local,tem_estoque,cores_com_estoque,paywall\n")
            f.write("{},{},{},{},{}\n".format(
                datetime.now().isoformat(timespec="seconds"),
                local, int(info["tem"]),
                "|".join(info["com_estoque"]), int(info["paywall"])))
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--intervalo", type=int, default=INTERVALO_PADRAO,
                   help=f"segundos entre checagens (minimo {INTERVALO_MINIMO})")
    p.add_argument("--teste", action="store_true",
                   help="dispara o alerta uma vez para validar som/notificacao")
    args = p.parse_args()

    if args.teste:
        alertar("TESTE", ALVOS[0][1], "isto e apenas um teste")
        return

    intervalo = max(args.intervalo, INTERVALO_MINIMO)
    if args.intervalo < INTERVALO_MINIMO:
        print(f"[aviso] intervalo elevado para o minimo de {INTERVALO_MINIMO}s.")

    estado = carregar_estado()
    falhas = 0

    print(f"Monitorando IBEX 450 em {len(ALVOS)} concessionarias, a cada ~{intervalo}s.")
    print(f"Historico: {ARQ_LOG}")
    print("Ctrl+C para parar.\n")

    while True:
        for local, url in ALVOS:
            try:
                info = analisar(baixar(url))
                falhas = 0

                if info["n_campos"] == 0:
                    print(f"[{agora()}] {local}: nao achei has_stock no HTML "
                          f"(o site pode ter mudado de formato)")
                    continue

                registrar(local, info)

                if info["cores"]:
                    resumo = ", ".join(
                        f"{nome}:{'SIM' if tem else 'nao'}" for nome, tem in info["cores"])
                else:
                    resumo = "sem estoque"
                flag = "  <<< SALA DE ESPERA ATIVA" if info["paywall"] else ""
                print(f"[{agora()}] {local}: {resumo}{flag}")

                ant = estado.get(local, {})
                antes_tem = ant.get("tem")
                antes_pw = ant.get("paywall")

                # BUG CORRIGIDO: alerta tambem na primeira leitura ja com estoque.
                # Sem isso, reiniciar o script durante um lote pequeno = silencio.
                if info["tem"] and antes_tem is not True:
                    cores = ", ".join(info["com_estoque"]) or "disponivel"
                    alertar(local, url, f"cores: {cores}")

                # BUG CORRIGIDO: paywall so alerta na virada, nao a cada ciclo.
                if info["paywall"] and antes_pw is not True:
                    alertar(local, url, "sala de espera ligada - lote abrindo agora")

                estado[local] = {"tem": info["tem"], "paywall": info["paywall"]}
                salvar_estado(estado)

            except urllib.error.HTTPError as e:
                print(f"[{agora()}] {local}: HTTP {e.code}")
                falhas += 1
            except Exception as e:
                print(f"[{agora()}] {local}: erro {type(e).__name__}: {e}")
                falhas += 1

            time.sleep(random.uniform(2, 5))   # espaca as duas urls

        espera = intervalo + random.uniform(-30, 30)
        if falhas >= 3:
            espera = min(espera * 3, 1800)
            print(f"  [aviso] varias falhas seguidas, aguardando {int(espera)}s")
        time.sleep(max(espera, 30))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nMonitor encerrado.")
