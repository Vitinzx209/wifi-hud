# WIFI HUD - diagnostico de conexao Wi-Fi no Windows
# Salva um relatorio na Area de Trabalho (ou na pasta do usuario).
# O diagnostico nao altera configuracoes. As opcoes do menu de reparos
# (renovar IP, limpar cache DNS e definir/restaurar DNS) so rodam se voce
# escolher; as de DNS pedem confirmacao e exigem administrador.

import codecs
import concurrent.futures
import os
import re
import socket
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime

# Cores ANSI globais usadas pela HUD e pelo menu de reparos.
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[90m"
RESET = "\033[0m"

IPV4_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
CONTINUATION_RE = re.compile(r"^\s{20,}\S")  # linhas de continuacao do ipconfig


def _console_encoding():
    """ipconfig e ping no Windows saem em codepage OEM (ex.: cp850 no Brasil)."""
    try:
        codecs.lookup("oem")  # disponivel no Python 3.10+ (Windows)
        return "oem"
    except LookupError:
        return "cp850"


OEM_ENCODING = _console_encoding()


def run_command_rc(args, timeout=12):
    """Executa um comando e devolve (codigo_de_saida, stdout + stderr)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding=OEM_ENCODING,
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except Exception as exc:
        return -1, f"ERRO ao executar {args[0]}: {exc}"


def run_command(args, timeout=12):
    """Executa um comando e devolve stdout + stderr sem abrir outra janela."""
    return run_command_rc(args, timeout)[1]


def get_ipconfig():
    # /all inclui adaptadores, DHCP, DNS e gateway.
    return run_command(["ipconfig", "/all"], timeout=15)


def valid_ipv4(value):
    try:
        parts = value.split(".")
        return len(parts) == 4 and all(0 <= int(part) <= 255 for part in parts)
    except Exception:
        return False


def parse_adapter_blocks(output):
    """Divide o ipconfig por cabecalho de adaptador (linha sem recuo terminada em ':').

    Retorna os blocos que parecem ser Wi-Fi; se nenhum parecer, retorna todos.
    """
    blocks, current = [], []
    for line in output.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    wifi_words = ("wi-fi", "wifi", "wireless", "wlan", "sem fio")
    wifi_blocks = [b for b in blocks if any(w in b.lower() for w in wifi_words)]
    return wifi_blocks or blocks


def extract_ipv4(block):
    # Evita confundir gateway e DNS com o IP do notebook.
    for line in block.splitlines():
        low = line.lower()
        if "ipv4" in low and "gateway" not in low:
            match = IPV4_RE.search(line)
            if match and valid_ipv4(match.group(0)):
                return match.group(0)
    return None


def extract_gateway(block):
    """Gateway pode vir em duas linhas (IPv6 primeiro, IPv4 na continuacao)."""
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if "gateway" in line.lower():
            chunk = [line]
            for nxt in lines[i + 1:]:
                if CONTINUATION_RE.match(nxt):
                    chunk.append(nxt)
                else:
                    break
            for item in chunk:
                match = IPV4_RE.search(item)
                if match and valid_ipv4(match.group(0)):
                    return match.group(0)
    return None


def extract_dns(block):
    values = []
    in_dns_section = False
    for line in block.splitlines():
        low = line.lower()
        if "servidores dns" in low or "dns servers" in low or "servidor dns" in low:
            in_dns_section = True
            match = IPV4_RE.search(line)
            if match and valid_ipv4(match.group(0)):
                values.append(match.group(0))
            continue
        if in_dns_section:
            if CONTINUATION_RE.match(line):
                match = IPV4_RE.search(line)
                if match and valid_ipv4(match.group(0)):
                    values.append(match.group(0))
            else:
                break
    return list(dict.fromkeys(values))


def ping(host):
    output = run_command(["ping", "-n", "2", "-w", "1200", host], timeout=8)
    success = bool(re.search(r"TTL\s*=", output, re.IGNORECASE))
    return success, output.strip()


def tcp_test(host="1.1.1.1", port=443, timeout=4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"Conexao TCP com {host}:{port} funcionou."
    except Exception as exc:
        return False, f"Conexao TCP com {host}:{port} falhou: {exc}"


def dns_test(timeout=5):
    # getaddrinfo nao tem timeout proprio; roda em thread para nao travar a HUD.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(socket.getaddrinfo, "www.microsoft.com", 443, 0, socket.SOCK_STREAM)
    try:
        addresses = future.result(timeout=timeout)
        ips = list(dict.fromkeys(item[4][0] for item in addresses))
        return True, "DNS resolveu www.microsoft.com: " + ", ".join(ips[:5])
    except concurrent.futures.TimeoutError:
        return False, f"DNS nao respondeu em {timeout}s ao resolver www.microsoft.com"
    except Exception as exc:
        return False, f"Falha ao resolver www.microsoft.com: {exc}"
    finally:
        executor.shutdown(wait=False)


def https_test():
    url = "https://www.microsoft.com/"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "WIFI-HUD/1.0"})
        with urllib.request.urlopen(request, timeout=8) as response:
            return True, f"HTTPS funcionou: HTTP {response.status} em {url}"
    except urllib.error.HTTPError as exc:
        # Resposta HTTP indica que DNS/TCP/TLS chegaram ao servidor/proxy.
        return True, f"HTTPS recebeu resposta HTTP {exc.code} em {url}"
    except Exception as exc:
        return False, f"Falha HTTPS em {url}: {type(exc).__name__}: {exc}"


def proxy_test():
    lines = []
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL"):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                    lines.append(f"{name} = {value}")
                except FileNotFoundError:
                    lines.append(f"{name} = (nao definido)")
    except Exception as exc:
        lines.append(f"Nao foi possivel ler proxy do usuario: {exc}")
    return lines


def choose_adapter_block(output):
    blocks = parse_adapter_blocks(output)
    # Prioriza o bloco com IPv4 utilizavel e gateway.
    for block in blocks:
        if extract_ipv4(block) and extract_gateway(block):
            return block
    for block in blocks:
        if extract_ipv4(block):
            return block
    # Sem IP em nenhum bloco: devolve o primeiro candidato (nunca a saida inteira,
    # para nao pegar o IP de outro adaptador como falso positivo).
    return blocks[0] if blocks else ""


MAC_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5}(?![-:0-9A-Fa-f])")


def extract_mac(text):
    """MAC (Endereco Fisico / Physical Address) em texto do ipconfig ou do netsh."""
    for line in (text or "").splitlines():
        low = line.lower()
        if "sico" in low or "physical" in low:
            match = MAC_RE.search(line)
            if match:
                return match.group(0).upper().replace(":", "-")
    return None


def mac_is_local(mac):
    """Bit 'localmente administrado' ligado: tipico de MAC aleatorio (ou virtual)."""
    try:
        return bool(int(mac.split("-")[0], 16) & 0x02)
    except Exception:
        return False


def parse_wlan_ssid(text):
    for line in (text or "").splitlines():
        match = re.match(r"^\s*SSID\s*:\s*(.+?)\s*$", line)  # nao casa com BSSID
        if match:
            return match.group(1)
    return None


def last_wlan_failure():
    """Ultimo evento de falha de conexao Wi-Fi (8002 / 11002) no log do Windows."""
    rc, out = run_command_rc(
        ["wevtutil", "qe", "Microsoft-Windows-WLAN-AutoConfig/Operational",
         "/q:*[System[(EventID=8002 or EventID=11002)]]", "/c:1", "/rd:true", "/f:text"],
        timeout=10,
    )
    text = out.strip()
    if rc != 0 or not text:
        return None
    return text


def summarize_event(text):
    """Data e motivo do evento, em uma linha (funciona em portugues e ingles)."""
    date = reason = None
    for line in text.splitlines():
        low = line.lower().strip()
        if date is None:
            match = re.match(r"^(?:date|data)\s*:\s*(.+)$", line.strip(), re.IGNORECASE)
            if match:
                date = match.group(1)
        if reason is None and ("reason" in low or "motivo" in low):
            reason = line.strip()
    parts = [p for p in (date, reason) if p]
    return " | ".join(parts) if parts else "evento encontrado (veja o relatorio)"


def wifi_info(adapter_block):
    """MAC, estado do Wi-Fi, endereco aleatorio e ultimo erro de conexao."""
    info = {"mac": extract_mac(adapter_block)}
    wlan = run_command(["netsh", "wlan", "show", "interfaces"], timeout=10).strip()
    info["wlan"] = wlan
    info["mac_wifi"] = extract_mac(wlan)
    info["mac_local"] = bool(mac_is_local(info["mac_wifi"] or info["mac"] or ""))
    info["ssid"] = parse_wlan_ssid(wlan)
    info["profile_random"] = []
    if info["ssid"]:
        profile = run_command(["netsh", "wlan", "show", "profile", f"name={info['ssid']}"], timeout=10)
        info["profile_random"] = [
            l.strip() for l in profile.splitlines()
            if "random" in l.lower() or "aleat" in l.lower()
        ]
    info["last_failure"] = last_wlan_failure()
    return info


def wifi_screen_lines(info):
    lines = [CYAN + "WI-FI / MAC" + RESET]
    mac = info.get("mac_wifi") or info.get("mac")
    if mac:
        extra = " (localmente administrado: provavelmente aleatorio)" if info.get("mac_local") else ""
        lines.append(f"MAC: {mac}{extra}")
    else:
        lines.append("MAC: nao identificado")
    lines.append("Rede Wi-Fi: " + (info.get("ssid") or "nenhuma conectada"))
    for text in info.get("profile_random", [])[:2]:
        lines.append("Perfil: " + text)
    if info.get("last_failure"):
        lines.append("Ultima falha Wi-Fi registrada: " + summarize_event(info["last_failure"]))
    return lines


def wifi_report_lines(info):
    lines = [
        f"MAC do adaptador analisado: {info.get('mac') or 'nao identificado'}",
        f"MAC da interface Wi-Fi: {info.get('mac_wifi') or 'nao identificado'}",
        "MAC localmente administrado (tipico de endereco aleatorio): "
        + ("sim" if info.get("mac_local") else "nao"),
        "Rede Wi-Fi atual (SSID): " + (info.get("ssid") or "nenhuma"),
    ]
    if info.get("profile_random"):
        lines += ["Perfil da rede:"] + ["  " + t for t in info["profile_random"]]
    else:
        lines.append("Perfil da rede: o Windows nao informou a opcao de endereco aleatorio.")
    lines += [
        "",
        "--- netsh wlan show interfaces ---",
        info.get("wlan") or "(sem saida)",
        "",
        "--- Ultima falha de conexao Wi-Fi (eventos 8002/11002) ---",
        info.get("last_failure") or "Nenhuma falha encontrada ou log inacessivel.",
        "",
        "Observacao: MAC aleatorio e falha registrada sao pistas; nao provam filtro de MAC.",
    ]
    return lines


def diagnose():
    """Executa os testes e devolve resultados estruturados."""
    full_ipconfig = get_ipconfig()
    adapter = choose_adapter_block(full_ipconfig)
    ipv4 = extract_ipv4(adapter)
    gateway = extract_gateway(adapter)
    dns_servers = extract_dns(adapter)
    apipa = bool(ipv4 and ipv4.startswith("169.254."))

    tests = []
    tests.append(("IPV4", bool(ipv4) and not apipa, f"IP do notebook: {ipv4 or 'nao identificado'}"))
    tests.append(("GATEWAY", bool(gateway), f"Gateway: {gateway or 'nao identificado'}"))

    if gateway:
        ok, detail = ping(gateway)
        tests.append(("PING GATEWAY", ok, detail))
    else:
        tests.append(("PING GATEWAY", False, "Pulou: gateway nao identificado."))

    ok, detail = ping("1.1.1.1")
    tests.append(("PING INTERNET", ok, detail))
    ok, detail = tcp_test()
    tests.append(("TCP 443", ok, detail))
    ok, detail = dns_test()
    tests.append(("DNS", ok, detail))
    ok, detail = https_test()
    tests.append(("HTTPS", ok, detail))
    proxy_lines = proxy_test()
    wifi = wifi_info(adapter)

    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ipv4": ipv4,
        "gateway": gateway,
        "dns_servers": dns_servers,
        "tests": tests,
        "proxy": proxy_lines,
        "wifi": wifi,
        "adapter": adapter or "(nenhum adaptador Wi-Fi identificado)",
        "ipconfig": full_ipconfig,
    }


def interpretation(result):
    """Explicacoes orientativas, sem presumir bloqueio como causa certa."""
    checks = {name: ok for name, ok, _ in result["tests"]}
    tips = []
    ip = result.get("ipv4") or ""
    gateway = result.get("gateway")
    if not ip:
        tips.append("Nao identifiquei um IPv4. Confira se o adaptador Wi-Fi esta conectado e execute o diagnostico novamente.")
    elif ip.startswith("169.254."):
        tips.append("IP 169.254.x.x: o Windows pode nao ter recebido IP do DHCP. Desconecte e reconecte ao Wi-Fi; se continuar, contate o suporte da escola.")
    if ip and not gateway:
        tips.append("IP sem gateway: pode ser uma concessao DHCP incompleta ou configuracao da rede. Reconecte ao Wi-Fi e confira o portal de acesso.")
    if gateway and not checks.get("PING GATEWAY", False):
        tips.append("O ping do gateway falhou. Isso nao prova falha local: alguns roteadores bloqueiam ping. Compare com TCP/DNS/HTTPS.")
    if not checks.get("DNS", False):
        tips.append("DNS falhou: tente abrir a pagina de login da rede (portal cativo) e reconectar ao Wi-Fi. Se o DNS da rede estiver com problema, a opcao [6] do menu define um DNS publico (exige administrador).")
    if not checks.get("TCP 443", False):
        tips.append("TCP 443 falhou: o acesso externo pode estar indisponivel ou filtrado. Verifique se outros sites funcionam e consulte o suporte da escola.")
    if not checks.get("HTTPS", False):
        tips.append("HTTPS falhou: pode ser portal cativo, proxy, certificado, firewall ou indisponibilidade do site. Confira data/hora do Windows e o portal de login.")
    wifi = result.get("wifi") or {}
    sem_internet = not checks.get("TCP 443", False) or not checks.get("DNS", False)
    if wifi.get("mac_local") and (not ip or ip.startswith("169.254.") or sem_internet):
        tips.append("O MAC em uso parece aleatorio (endereco de hardware aleatorio do Windows). Se a rede usa lista de MACs liberados, isso pode impedir o acesso; veja Configuracoes > Rede > Wi-Fi > propriedades da rede. Isso nao prova filtro de MAC.")
    if checks.get("TCP 443") and not checks.get("HTTPS", False):
        tips.append("TCP 443 passou mas HTTPS falhou: o teste HTTPS usa o proxy do sistema e o teste TCP nao, entao essa diferenca pode indicar proxy.")
    if checks.get("TCP 443") and checks.get("DNS") and checks.get("HTTPS"):
        tips.append("TCP, DNS e HTTPS passaram. Se um aplicativo especifico nao conecta, a falha pode estar no proprio aplicativo ou no servico.")
    if not tips:
        tips.append("Os testes basicos nao apontaram um problema claro. Rode novamente e compare com um dispositivo que funciona na mesma rede.")
    return tips


def report_text(result):
    lines = [
        "WIFI HUD - RELATORIO DE DIAGNOSTICO",
        f"Data/hora: {result['timestamp']}",
        "",
        "=== RESUMO ===",
    ]
    for name, ok, detail in result["tests"]:
        lines.append(f"[{'OK' if ok else 'FALHOU/NAO IDENTIFICADO'}] {name}: {detail}")
    lines += [
        "",
        "=== ADAPTADOR SELECIONADO ===",
        result["adapter"],
        "",
        "=== IP CONFIG COMPLETO ===",
        result["ipconfig"],
        "",
        "=== SERVIDORES DNS ===",
        ", ".join(result["dns_servers"]) if result["dns_servers"] else "Nao identificados",
        "",
        "=== PROXY DO USUARIO ===",
        *result["proxy"],
        "",
        "=== WI-FI / MAC ===",
        *wifi_report_lines(result.get("wifi") or {}),
        "",
        "=== SUGESTOES ===",
        *[f"- {tip}" for tip in interpretation(result)],
        "",
        "Observacao: o teste HTTPS usa o proxy do sistema; o teste TCP nao.",
        "Resultados indicam pistas, nao provam por si so a causa nem um bloqueio intencional.",
    ]
    return "\n".join(lines)


def get_desktop_dir():
    """Pergunta ao Windows onde fica a Area de Trabalho (funciona com OneDrive)."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Desktop = {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        folder_id = GUID(
            0xB4BFCC3A, 0xDB2C, 0x424C,
            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
        )
        path_ptr = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr)
        )
        if result == 0 and path_ptr.value:
            path = path_ptr.value
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            if os.path.isdir(path):
                return path
    except Exception:
        pass
    home = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")
    return desktop if os.path.isdir(desktop) else home


def save_report(result):
    output_dir = get_desktop_dir()
    # Historico local: um arquivo por diagnostico, para comparar depois.
    history_dir = os.path.join(output_dir, "WIFI_HUD_Historico")
    os.makedirs(history_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(history_dir, f"diagnostico_wifi_{stamp}.txt")
    latest_path = os.path.join(output_dir, "diagnostico_wifi.txt")
    content = report_text(result)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(content)
    return report_path, latest_path


DNS_PUBLICOS = ("1.1.1.1", "8.8.8.8")  # Cloudflare e Google

# Cabecalho do adaptador no ipconfig, em portugues e ingles.
ADAPTER_HEADER_RE = re.compile(
    r"^(?:Adaptador(?:\s+de)?(?:\s+(?:Rede|LAN))?(?:\s+sem\s+fio)?(?:\s+Ethernet)?"
    r"|Wireless LAN adapter|Ethernet adapter)\s+(.+?):\s*$",
    re.IGNORECASE,
)


def adapter_name(adapter_block):
    """Extrai o nome da interface (ex.: 'Wi-Fi') do cabecalho do ipconfig."""
    lines = (adapter_block or "").splitlines()
    match = ADAPTER_HEADER_RE.match(lines[0].strip()) if lines else None
    return match.group(1).strip() if match else None


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Reabre o programa pedindo elevacao (o Windows mostra o aviso do UAC)."""
    try:
        import ctypes
        script = os.path.abspath(sys.argv[0])
        params = " ".join(f'"{a}"' for a in [script] + sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        return rc > 32
    except Exception:
        return False


def confirm(message):
    try:
        return input(f"  {message} (s/n): ").strip().lower() in ("s", "sim", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def ask_adapter_name():
    try:
        return input("  Nao identifiquei o adaptador. Digite o nome (ex.: Wi-Fi): ").strip() or None
    except (EOFError, KeyboardInterrupt):
        return None


def save_dns_backup(name, text):
    """Guarda a configuracao de DNS de antes da mudanca."""
    try:
        history_dir = os.path.join(get_desktop_dir(), "WIFI_HUD_Historico")
        os.makedirs(history_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(history_dir, f"dns_antes_{stamp}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"Interface: {name}\n\n{text}")
        return path
    except Exception:
        return None


def show_dns(name):
    print(YELLOW + f"\n  DNS atual de '{name}':" + RESET)
    print(run_command(["netsh", "interface", "ip", "show", "dnsservers", f"name={name}"], timeout=10))


def set_public_dns(name):
    """Define DNS publico no adaptador. Funciona sem internet (e config local)."""
    if not is_admin():
        print(RED + "  Precisa de administrador para mudar o DNS. Use a opcao [8] para reabrir como administrador." + RESET)
        return
    print(f"\n  Isso muda o DNS de '{name}' para {DNS_PUBLICOS[0]} e {DNS_PUBLICOS[1]}.")
    print(DIM + "  Em rede de escola/empresa pode ferir as regras de uso; e se o roteador filtra DNS externo, a mudanca pode nao resolver." + RESET)
    if not confirm("Continuar?"):
        print("  Cancelado.")
        return
    _, before = run_command_rc(["netsh", "interface", "ip", "show", "dnsservers", f"name={name}"], timeout=10)
    backup = save_dns_backup(name, before)
    rc, out = run_command_rc(
        ["netsh", "interface", "ip", "set", "dnsservers", f"name={name}",
         "source=static", f"address={DNS_PUBLICOS[0]}", "validate=no"],
        timeout=15,
    )
    if rc != 0:
        print(RED + "  Falhou ao definir o DNS principal:" + RESET)
        print(out.strip())
        return
    rc, out = run_command_rc(
        ["netsh", "interface", "ip", "add", "dnsservers", f"name={name}",
         f"address={DNS_PUBLICOS[1]}", "index=2", "validate=no"],
        timeout=15,
    )
    if rc != 0:
        print(YELLOW + "  O DNS secundario nao foi adicionado:" + RESET)
        print(out.strip())
    run_command(["ipconfig", "/flushdns"], timeout=10)
    print(GREEN + "  DNS definido e cache limpo." + RESET)
    if backup:
        print("  Configuracao anterior salva em: " + backup)
    print("  Para desfazer, use a opcao [7]. Sem internet, o teste de DNS so passa quando a rede voltar.")


def restore_dns(name):
    """Volta o DNS do adaptador para automatico (DHCP)."""
    if not is_admin():
        print(RED + "  Precisa de administrador para mudar o DNS. Use a opcao [8] para reabrir como administrador." + RESET)
        return
    rc, out = run_command_rc(
        ["netsh", "interface", "ip", "set", "dnsservers", f"name={name}", "source=dhcp"],
        timeout=15,
    )
    if rc != 0:
        print(RED + "  Falhou ao restaurar o DNS:" + RESET)
        print(out.strip())
        return
    run_command(["ipconfig", "/flushdns"], timeout=10)
    print(GREEN + f"  DNS de '{name}' voltou para automatico (DHCP)." + RESET)


def print_repair_menu(name):
    print()
    print(CYAN + "  " + "─" * 60 + RESET)
    admin = "sim" if is_admin() else "nao"
    print(CYAN + "  REPAROS" + RESET + DIM + f"  (adaptador: {name or 'nao identificado'} | administrador: {admin})" + RESET)
    print("  [1] Rodar diagnostico novamente")
    print("  [2] Renovar IP (ipconfig /renew) - pode exigir administrador")
    print("  [3] Limpar cache DNS (ipconfig /flushdns) - pode exigir administrador")
    print("  [4] Abrir configuracoes de Wi-Fi")
    print("  [5] Ver DNS atual do adaptador")
    print(f"  [6] Definir DNS publico ({DNS_PUBLICOS[0]} e {DNS_PUBLICOS[1]}) - exige administrador")
    print("  [7] Restaurar DNS automatico (DHCP) - exige administrador")
    print("  [8] Reabrir como administrador")
    print("  [9] Sair")


def repair_menu(result):
    """Acoes opcionais; so rodam se voce escolher.

    Depois de cada acao o menu volta sozinho, sem pedir Enter, para que
    outra opcao possa ser escolhida em seguida.
    """
    name = adapter_name(result.get("adapter", ""))
    while True:
        print_repair_menu(name)
        try:
            choice = input("  Escolha: ").strip()
        except (EOFError, KeyboardInterrupt):
            return "exit"

        if choice == "1":
            return "diagnose"
        elif choice == "2":
            print(YELLOW + "\n  Tentando renovar o IP... A conexao pode cair por alguns segundos." + RESET)
            print(run_command(["ipconfig", "/renew"], timeout=25))
            print(GREEN + "  Renovacao concluida. Escolha outra opcao abaixo." + RESET)
        elif choice == "3":
            print(YELLOW + "\n  Limpando cache DNS..." + RESET)
            print(run_command(["ipconfig", "/flushdns"], timeout=10))
            print(GREEN + "  Limpeza concluida. Escolha outra opcao abaixo." + RESET)
        elif choice == "4":
            try:
                os.startfile("ms-settings:network-wifi")
                print(GREEN + "  Configuracoes de Wi-Fi solicitadas." + RESET)
            except Exception as exc:
                print(RED + f"  Nao consegui abrir configuracoes: {exc}" + RESET)
        elif choice in ("5", "6", "7"):
            name = name or ask_adapter_name()
            if not name:
                print(RED + "  Sem o nome do adaptador nao da para continuar." + RESET)
                continue
            if choice == "5":
                show_dns(name)
            elif choice == "6":
                set_public_dns(name)
            else:
                restore_dns(name)
        elif choice == "8":
            if is_admin():
                print("  Ja esta rodando como administrador.")
            elif relaunch_as_admin():
                print(GREEN + "  Pedido enviado. Confirme o aviso do Windows; uma nova janela vai abrir." + RESET)
                return "exit"
            else:
                print(RED + "  Nao consegui reabrir como administrador (ou o aviso foi recusado)." + RESET)
        elif choice == "9":
            return "exit"
        else:
            print(RED + "  Opcao invalida. Digite um numero de 1 a 9." + RESET)


def main():
    os.system("")  # habilita sequencias ANSI no console do Windows 10
    os.system("title WIFI HUD - Network Diagnostics")
    WIDTH = 64

    def line(char="─"):
        print(CYAN + char * WIDTH + RESET)

    def banner():
        os.system("cls")
        print(GREEN + "╔" + "═" * (WIDTH - 2) + "╗")
        print("║" + " " * (WIDTH - 2) + "║")
        print("║" + "WIFI HUD // NETWORK DIAGNOSTICS".center(WIDTH - 2) + "║")
        print("║" + " " * (WIDTH - 2) + "║")
        print("╚" + "═" * (WIDTH - 2) + "╝" + RESET)

    while True:
        banner()
        print(DIM + "  Os testes locais funcionam sem internet; testes externos precisam de rede." + RESET)
        result = diagnose()
        for name, ok, detail in result["tests"]:
            color = GREEN if ok else RED
            tag = " OK " if ok else "FALHA"
            # Mostra so a primeira linha do detalhe (o ping devolve varias).
            short = detail.splitlines()[0] if detail else ""
            print(f"  {color}[{tag}]{RESET} {name:<17} {short}")
        print()
        for text in wifi_screen_lines(result.get("wifi") or {}):
            print("  " + text)
        print()
        line()
        print(CYAN + "  INTERPRETACAO AUTOMATICA" + RESET)
        for tip in interpretation(result):
            print("  " + YELLOW + "• " + RESET + tip)
        print()
        try:
            report_path, latest_path = save_report(result)
            print(GREEN + "  RELATORIOS SALVOS" + RESET)
            print("  Atual:     " + latest_path)
            print("  Historico: " + report_path)
        except Exception as exc:
            print(RED + f"  Erro ao salvar relatorio: {exc}" + RESET)

        choice = repair_menu(result)
        if choice == "exit":
            print("\n  WIFI HUD encerrado.")
            break
        # Diagnostico novamente volta ao topo e atualiza o relatorio.


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Interrompido pelo usuario.")
    except Exception as exc:
        print("\n[ERRO] O programa encontrou um problema:")
        print(f"{type(exc).__name__}: {exc}")
        print("\nEnvie esta mensagem para diagnostico.")
    finally:
        try:
            input("\nWIFI HUD finalizado. Pressione Enter para fechar...")
        except (EOFError, KeyboardInterrupt):
            pass
