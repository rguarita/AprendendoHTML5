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

Telegram (opcional):
    export TELEGRAM_BOT_TOKEN="123456:ABC..."
    export TELEGRAM_CHAT_ID="987654321"

ntfy (opcional, alcanca PC e outros aparelhos, sem cadastro):
    export NTFY_TOPIC="um-nome-secreto-e-dificil-de-adivinhar"
    # export NTFY_SERVER="https://ntfy.sh"   # ou a sua propria instancia

WhatsApp via CallMeBot (opcional):
    export CALLMEBOT_PHONE="+5519999999999"
    export CALLMEBOT_APIKEY="123456"
"""

import argparse
import gzip
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
import urllib.parse
import urllib.request
from datetime import datetime

ALVOS = [
    ("Piracicaba", "https://reserva.cfmoto.com.br/produto/motorcycles-ibex-450/cfmoto-virage-piracicaba"),
    ("Sao Paulo",  "https://reserva.cfmoto.com.br/produto/motorcycles-ibex-450/cfmoto-c10-sao-paulo"),
]

# Intervalo minimo em segundos. Bater mais forte que isso no site nao te da
# vantagem nenhuma e e a forma mais rapida de tomar bloqueio de IP.
INTERVALO_MINIMO = 60
INTERVALO_PADRAO = 300
# No Wi-Fi nao ha custo de dados nem de rede movel, entao vale checar mais
# rapido: e onde se ganha chance de pegar uma desistencia, que aparece a
# qualquer hora e pode ficar pouco tempo no ar. O piso de 5s existe porque
# abaixo disso o ganho e nulo (o Pix leva minutos) e o risco de bloqueio por
# WAF deixa de ser teorico.
INTERVALO_WIFI = 5
WIFI_MINIMO = 5
# No modo turbo aceitamos um intervalo bem menor, mas por tempo limitado.
# Abaixo de 5s o ganho e nulo (quem demora e o humano, nao o script) e o risco
# de bloqueio por WAF passa a ser concreto.
TURBO_MINIMO = 5
TURBO_PADRAO = 10
TURBO_DURACAO_PADRAO = 20

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
    """Pede gzip: a pagina cai de ~51 KB para ~18 KB. Em dados moveis, rodando
    o dia inteiro, isso e a diferenca entre ~890 MB e ~300 MB por mes."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Accept-Encoding": "gzip",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        dados = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            dados = gzip.decompress(dados)
        return dados.decode("utf-8", errors="replace")


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
        print("  [ok] Telegram enviado.")
    except urllib.error.HTTPError as e:
        # a API do Telegram explica o motivo no corpo; sem isso o diagnostico
        # vira adivinhacao na hora de configurar
        try:
            corpo = json.loads(e.read().decode())
            motivo = corpo.get("description", "")
        except Exception:
            motivo = ""
        print(f"  [erro] Telegram HTTP {e.code}: {motivo}")
        if "chat not found" in motivo.lower():
            print("         -> abra uma conversa com o SEU bot e envie /start antes.")
        elif "unauthorized" in motivo.lower():
            print("         -> TELEGRAM_BOT_TOKEN invalido ou incompleto.")
    except Exception as e:
        print(f"  [erro] Telegram falhou: {type(e).__name__}: {e}")


def em_termux():
    """Termux se identifica como Linux, mas nao tem notify-send; o que ele tem
    e o termux-notification, do pacote termux-api."""
    return bool(shutil.which("termux-notification"))


def ntfy(titulo, msg, url=None):
    """Publica em um topico do ntfy (https://ntfy.sh por padrao). Nao exige
    cadastro nem telefone: o topico e a credencial, entao escolha um nome
    dificil de adivinhar - qualquer pessoa que saiba o nome recebe (e pode
    enviar) mensagens nele. NTFY_SERVER permite apontar para uma instancia
    propria."""
    topico = os.environ.get("NTFY_TOPIC")
    if not topico:
        return
    servidor = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        # cabecalhos HTTP nao aceitam bem nao-ASCII; o corpo vai em UTF-8
        def ascii_seguro(t):
            return t.encode("ascii", "ignore").decode("ascii")

        cab = {
            "Title": ascii_seguro(titulo),
            "Priority": "urgent",
            "Tags": "rotating_light,motorcycle",
        }
        if url:
            cab["Click"] = url
        req = urllib.request.Request(f"{servidor}/{topico}",
                                     data=msg.encode("utf-8"), headers=cab)
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        print("  [ok] ntfy enviado.")
    except urllib.error.HTTPError as e:
        print(f"  [erro] ntfy HTTP {e.code} - confira NTFY_TOPIC.")
    except Exception as e:
        print(f"  [erro] ntfy falhou: {type(e).__name__}: {e}")


def whatsapp(msg):
    """Envia via CallMeBot, que e um servico de terceiros: a mensagem passa
    pelo servidor deles. O conteudo aqui e so o aviso de estoque, sem nada
    sensivel. E um canal secundario - a notificacao local do Android continua
    sendo a mais rapida, por nao depender de rede."""
    fone = os.environ.get("CALLMEBOT_PHONE")
    chave = os.environ.get("CALLMEBOT_APIKEY")
    if not (fone and chave):
        return
    try:
        url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
            {"phone": fone, "text": msg, "apikey": chave})
        with urllib.request.urlopen(url, timeout=20) as r:
            corpo = r.read().decode("utf-8", errors="replace")
        baixo = corpo.lower()
        if "queued" in baixo or "message sent" in baixo:
            print("  [ok] WhatsApp enviado.")
        else:
            # a resposta vem em HTML; sem limpar, o erro fica ilegivel
            limpo = re.sub(r"<[^>]+>", " ", corpo)
            limpo = re.sub(r"\s+", " ", limpo).strip()
            if "apikey" in baixo:
                print("  [erro] WhatsApp: apikey invalida ou nao autorizada "
                      "para este numero.")
            else:
                print(f"  [erro] WhatsApp: {limpo[:130]}")
    except urllib.error.HTTPError as e:
        print(f"  [erro] WhatsApp HTTP {e.code} - confira phone e apikey.")
    except Exception as e:
        print(f"  [erro] WhatsApp falhou: {type(e).__name__}: {e}")


def notificar_desktop(titulo, msg):
    so = platform.system()
    try:
        if em_termux():
            subprocess.run([
                "termux-notification",
                "--title", titulo,
                "--content", msg,
                "--priority", "max",
                "--sound",
                "--vibrate", "800,400,800,400,800",
                "--id", "cfmoto",
            ], check=False, timeout=15)
            return
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


def no_wifi():
    """True se o Android estiver em Wi-Fi. Fora do Termux, ou se a deteccao
    falhar, devolve None e o chamador mantem o intervalo conservador.

    Nao exige SSID: a partir do Android 8.1 ler o nome da rede depende de
    permissao de localizacao, e sem ela o sistema devolve "<unknown ssid>"
    mesmo com o Wi-Fi conectado. O estado do supplicant e o IP nao dependem
    dessa permissao, entao sao sinais mais confiaveis.
    """
    if not shutil.which("termux-wifi-connectioninfo"):
        return None
    try:
        saida = subprocess.run(["termux-wifi-connectioninfo"],
                               capture_output=True, timeout=10, text=True).stdout
        info = json.loads(saida)
    except Exception:
        return None

    estado = (info.get("supplicant_state") or "").upper()
    if estado == "COMPLETED":
        return True
    if estado in ("DISCONNECTED", "INACTIVE", "SCANNING", "INTERFACE_DISABLED"):
        return False

    # sem supplicant_state utilizavel, aceita indicios de conexao ativa
    ip = (info.get("ip") or "").strip()
    if ip and ip not in ("0.0.0.0", "<unknown>"):
        return True
    if isinstance(info.get("link_speed_mbps"), int) and info["link_speed_mbps"] > 0:
        return True
    ssid = (info.get("ssid") or "").strip()
    if ssid and ssid != "<unknown ssid>":
        return True
    return None


def travar_suspensao():
    """O Android mata processos em segundo plano. Sem o wake lock, o monitor
    morre quando a tela apaga - e voce nem fica sabendo."""
    if em_termux() and shutil.which("termux-wake-lock"):
        try:
            subprocess.run(["termux-wake-lock"], check=False, timeout=10)
            print("[termux] wake lock ativado (o monitor sobrevive a tela apagada).")
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
    whatsapp(msg)
    ntfy("CFMOTO IBEX 450 liberou!", f"{local} - {detalhe}", url)


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
    # Redirecionado para arquivo, o Python bufferiza a saida em blocos de 4 KB
    # e o log fica vazio por varios minutos, dando a impressao de que o monitor
    # nao esta rodando. Linha a linha, o `tail` mostra o estado na hora.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    p = argparse.ArgumentParser()
    p.add_argument("--intervalo", type=int, default=INTERVALO_PADRAO,
                   help=f"segundos entre checagens (minimo {INTERVALO_MINIMO})")
    p.add_argument("--teste", action="store_true",
                   help="dispara o alerta uma vez para validar som/notificacao")
    p.add_argument("--turbo", nargs="?", type=int, const=TURBO_PADRAO,
                   help=f"checa a cada N segundos (min {TURBO_MINIMO}, padrao "
                        f"{TURBO_PADRAO}) durante a janela de abertura de lote")
    p.add_argument("--intervalo-wifi", type=int, default=INTERVALO_WIFI,
                   help=f"intervalo usado quando o celular esta no Wi-Fi "
                        f"(padrao {INTERVALO_WIFI}s, minimo {WIFI_MINIMO}s); "
                        f"so vale no Termux")
    p.add_argument("--intervalo-fixo", action="store_true",
                   help="ignora a deteccao de Wi-Fi e usa sempre --intervalo")
    p.add_argument("--turbo-min", type=int, default=TURBO_DURACAO_PADRAO,
                   help=f"duracao do turbo em minutos (padrao {TURBO_DURACAO_PADRAO})")
    args = p.parse_args()

    if args.teste:
        alertar("TESTE", ALVOS[0][1], "isto e apenas um teste")
        return

    intervalo = max(args.intervalo, INTERVALO_MINIMO)
    if args.intervalo < INTERVALO_MINIMO:
        print(f"[aviso] intervalo elevado para o minimo de {INTERVALO_MINIMO}s.")

    travar_suspensao()
    estado = carregar_estado()
    falhas = 0

    turbo_ate = 0
    if args.turbo:
        turbo_seg = max(args.turbo, TURBO_MINIMO)
        turbo_ate = time.time() + args.turbo_min * 60
        if args.turbo < TURBO_MINIMO:
            print(f"[aviso] turbo elevado para o minimo de {TURBO_MINIMO}s.")
        print(f"TURBO: checando a cada {turbo_seg}s pelos proximos "
              f"{args.turbo_min} min, depois volta para {intervalo}s.")

    print(f"Monitorando IBEX 450 em {len(ALVOS)} concessionarias, a cada ~{intervalo}s.")
    print(f"Historico: {ARQ_LOG}")
    print("Ctrl+C para parar.\n")

    ritmo_atual = intervalo
    rede_antes = None

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

            # espaca as duas urls, mas sem estourar o ritmo pedido: com
            # intervalo curto, um intervalo fixo de 2-5s dobraria o ciclo
            rapido = turbo_ate > time.time() or ritmo_atual <= 30
            time.sleep(random.uniform(0.4, 0.9) if rapido
                       else random.uniform(2, 5))

        if turbo_ate > time.time():
            espera = turbo_seg
        else:
            if turbo_ate:
                print(f"[{agora()}] janela do turbo encerrada, voltando a {intervalo}s.")
                turbo_ate = 0
            base = intervalo
            if not args.intervalo_fixo:
                wifi = no_wifi()
                if wifi is True:
                    base = max(args.intervalo_wifi, WIFI_MINIMO)
                elif wifi is False:
                    base = intervalo
                if wifi != rede_antes:
                    nome = {True: "Wi-Fi", False: "dados moveis",
                            None: "indeterminada (sem termux-api?)"}[wifi]
                    print(f"[rede] {nome} -> intervalo de {int(base)}s")
                    rede_antes = wifi
            espera = base + random.uniform(-min(30, base / 4), min(30, base / 4))
        if falhas >= 3:
            espera = min(espera * 3, 1800)
            print(f"  [aviso] varias falhas seguidas, aguardando {int(espera)}s")
        ritmo_atual = espera
        time.sleep(max(espera, TURBO_MINIMO))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nMonitor encerrado.")
