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
    """Devolve (tem_estoque, variantes_com_estoque, total_variantes, paywall)."""
    estoques = [m == "true" for m in RE_STOCK.findall(html)]
    paywall = RE_PAYWALL.search(html)
    paywall_ativo = bool(paywall and paywall.group(1) == "true")
    return any(estoques), sum(estoques), len(estoques), paywall_ativo


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
    anterior = {local: None for local, _ in ALVOS}
    falhas = 0

    print(f"Monitorando IBEX 450 em {len(ALVOS)} concessionarias, a cada ~{intervalo}s.")
    print("Ctrl+C para parar.\n")

    while True:
        for local, url in ALVOS:
            try:
                tem, n_com, n_tot, paywall = analisar(baixar(url))
                falhas = 0

                if n_tot == 0:
                    print(f"[{agora()}] {local}: nao achei has_stock no HTML "
                          f"(o site pode ter mudado de formato)")
                    continue

                estado = f"{n_com}/{n_tot} cores com estoque"
                flag = " | SALA DE ESPERA ATIVA (lote comecando!)" if paywall else ""
                print(f"[{agora()}] {local}: {estado}{flag}")

                # so alerta na virada de indisponivel -> disponivel
                if tem and anterior[local] is False:
                    alertar(local, url, estado)
                elif paywall and anterior[local] is not None:
                    alertar(local, url, "sala de espera ligada - lote abrindo")

                anterior[local] = tem

            except urllib.error.HTTPError as e:
                print(f"[{agora()}] {local}: HTTP {e.code}")
                falhas += 1
            except Exception as e:
                print(f"[{agora()}] {local}: erro {type(e).__name__}: {e}")
                falhas += 1

            time.sleep(random.uniform(2, 5))   # espaca as duas urls

        # recuo progressivo se o site estiver fora do ar
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
