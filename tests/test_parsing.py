import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import wifi_hud as w  # noqa: E402

IPCONFIG_PT = """Configuração de IP do Windows

   Nome do Host. . . . . . . . . . . : PC

Adaptador Ethernet Ethernet:

   Endereço IPv4. . . . . . . . . . . . : 10.0.0.5(Preferencial)
   Gateway Padrão. . . . . . . . . . . . : 10.0.0.1

Adaptador de Rede sem Fio Wi-Fi:

   Descrição . . . . . . . . . . . . . . : Intel(R) Wireless-AC
   Endereço Físico . . . . . . . . . . . : 6A-3B-11-2C-9D-EE
   DUID de Cliente DHCPv6. . . . . . . . : 00-01-00-01-2A-4B-5C-6D-7E-8F-90-A1
   Endereço IPv4. . . . . . . . . . . . : 192.168.0.23(Preferencial)
   Máscara de Sub-rede . . . . . . . . . : 255.255.255.0
   Gateway Padrão. . . . . . . . . . . . : fe80::1%12
                                           192.168.0.1
   Servidor DHCP . . . . . . . . . . . . : 192.168.0.1
   Servidores DNS. . . . . . . . . . . . : 8.8.8.8
                                           8.8.4.4
   NetBIOS em TCP/IP . . . . . . . . . . : Ativado
"""

WLAN_INTERFACES = """Há 1 interface no sistema:

    Nome                   : Wi-Fi
    Endereço físico        : 6a:3b:11:2c:9d:ee
    Estado                 : conectado
    SSID                   : MinhaRede
    BSSID                  : aa:bb:cc:dd:ee:ff
"""

EVENT_TEXT = """Event[0]:
  Date: 2026-10-22T15:10:01.123
  Event ID: 8002
  Failure Reason: AP denied association (Reason Code: 0x30002)
"""


class AdapterParsingTests(unittest.TestCase):
    def test_picks_wifi_block_not_ethernet(self):
        block = w.choose_adapter_block(IPCONFIG_PT)
        self.assertTrue(block.startswith("Adaptador de Rede sem Fio Wi-Fi:"))

    def test_ipv4(self):
        block = w.choose_adapter_block(IPCONFIG_PT)
        self.assertEqual(w.extract_ipv4(block), "192.168.0.23")

    def test_gateway_on_continuation_line(self):
        block = w.choose_adapter_block(IPCONFIG_PT)
        self.assertEqual(w.extract_gateway(block), "192.168.0.1")

    def test_dns_multiple_lines(self):
        block = w.choose_adapter_block(IPCONFIG_PT)
        self.assertEqual(w.extract_dns(block), ["8.8.8.8", "8.8.4.4"])

    def test_no_blocks_returns_empty_not_whole_output(self):
        self.assertEqual(w.choose_adapter_block(""), "")

    def test_adapter_name(self):
        cases = {
            "Adaptador Ethernet Ethernet:": "Ethernet",
            "Adaptador de Rede sem Fio Wi-Fi:": "Wi-Fi",
            "Adaptador Ethernet Ethernet 2:": "Ethernet 2",
            "Wireless LAN adapter Wi-Fi:": "Wi-Fi",
            "Ethernet adapter Ethernet:": "Ethernet",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(w.adapter_name(header + "\n   x"), expected)

    def test_valid_ipv4(self):
        self.assertTrue(w.valid_ipv4("192.168.0.1"))
        self.assertFalse(w.valid_ipv4("256.1.1.1"))
        self.assertFalse(w.valid_ipv4("1.2.3"))


class MacTests(unittest.TestCase):
    def test_mac_from_ipconfig_ignores_duid(self):
        block = w.choose_adapter_block(IPCONFIG_PT)
        self.assertEqual(w.extract_mac(block), "6A-3B-11-2C-9D-EE")

    def test_mac_from_netsh_is_normalized(self):
        self.assertEqual(w.extract_mac(WLAN_INTERFACES), "6A-3B-11-2C-9D-EE")

    def test_locally_administered_bit(self):
        self.assertTrue(w.mac_is_local("6A-3B-11-2C-9D-EE"))
        self.assertFalse(w.mac_is_local("00-1A-2B-3C-4D-5E"))

    def test_ssid_does_not_match_bssid(self):
        self.assertEqual(w.parse_wlan_ssid(WLAN_INTERFACES), "MinhaRede")

    def test_event_summary(self):
        summary = w.summarize_event(EVENT_TEXT)
        self.assertIn("2026-10-22", summary)
        self.assertIn("AP denied association", summary)


class InterpretationTests(unittest.TestCase):
    def _result(self, tcp, dns, https, mac_local):
        return {
            "ipv4": "192.168.0.23",
            "gateway": "192.168.0.1",
            "wifi": {"mac_local": mac_local},
            "tests": [
                ("PING GATEWAY", True, ""),
                ("TCP 443", tcp, ""),
                ("DNS", dns, ""),
                ("HTTPS", https, ""),
            ],
        }

    def test_random_mac_hint_only_when_connection_fails(self):
        failing = w.interpretation(self._result(False, False, False, True))
        working = w.interpretation(self._result(True, True, True, True))
        self.assertTrue(any("MAC em uso" in t for t in failing))
        self.assertFalse(any("MAC em uso" in t for t in working))

    def test_all_ok_message(self):
        tips = w.interpretation(self._result(True, True, True, False))
        self.assertTrue(any("TCP, DNS e HTTPS passaram" in t for t in tips))


if __name__ == "__main__":
    unittest.main()
