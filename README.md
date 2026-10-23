# WIFI HUD

Diagnóstico de conexão Wi-Fi no Windows, direto no terminal. Roda uma série de testes em camadas (IP, gateway, ping, TCP, DNS e HTTPS), interpreta o resultado e salva um relatório para você comparar depois.

<!-- Adicione um print da tela: salve em docs/screenshot.png e descomente a linha abaixo
![WIFI HUD](docs/screenshot.png)
-->

## O que ele faz

- Identifica o adaptador Wi-Fi, o IPv4, o gateway e os servidores DNS a partir do `ipconfig /all`.
- Testa cada camada separadamente, para mostrar **onde** a conexão falha:

| Teste | O que verifica |
|---|---|
| `IPV4` | Se o Windows recebeu um IP válido (e não um `169.254.x.x`) |
| `GATEWAY` / `PING GATEWAY` | Se o roteador foi encontrado e responde |
| `PING INTERNET` | Se há saída para a internet por IP (`1.1.1.1`) |
| `TCP 443` | Se uma conexão TCP é aceita |
| `DNS` | Se nomes são resolvidos (com limite de tempo) |
| `HTTPS` | Se uma requisição HTTPS completa funciona |

- Mostra o **MAC** da interface, se ele parece ser um endereço aleatório do Windows, a rede atual e o último erro de conexão Wi-Fi registrado no log do sistema.
- Gera dicas de interpretação em linguagem simples, sem afirmar uma causa que os testes não provam.
- Salva um relatório (`diagnostico_wifi.txt`) e um histórico com data e hora na Área de Trabalho.

### Menu de reparos

Depois do diagnóstico, um menu volta sozinho após cada ação:

1. Rodar o diagnóstico de novo
2. Renovar IP (`ipconfig /renew`)
3. Limpar cache DNS (`ipconfig /flushdns`)
4. Abrir as configurações de Wi-Fi
5. Ver o DNS atual do adaptador
6. Definir DNS público (`1.1.1.1` e `8.8.8.8`)
7. Restaurar DNS automático (DHCP)
8. Reabrir como administrador
9. Sair

As ações de DNS (6 e 7) exigem administrador, pedem confirmação e, antes de mudar, salvam a configuração anterior em um arquivo.

## Como usar

Requisitos: **Windows 10 ou 11** e **Python 3.8+** (3.10+ recomendado). Não há dependências externas.

```
python wifi_hud.py
```

Para as opções que alteram o DNS, abra o terminal como administrador ou use a opção `[8]` do menu.

## Testes

Os testes cobrem a leitura do `ipconfig`, do MAC e do nome do adaptador, e rodam em qualquer sistema (não precisam do Windows):

```
python -m unittest discover -s tests -v
```

## Limitações

- Funciona **somente no Windows**: depende de `ipconfig`, `netsh`, `wevtutil` e do registro do Windows.
- A leitura do `ipconfig` foi pensada para saída em português e em inglês; outros idiomas podem não ser reconhecidos.
- Os resultados são **pistas**, não provas. Por exemplo, um ping que falha pode ser só um roteador que bloqueia ICMP, e um MAC aleatório não prova que existe filtro de MAC.
- Trocar o DNS no computador **não contorna** filtros feitos no roteador ou no firewall da rede.

## Uso responsável

Use em redes suas ou com permissão de quem as administra. Em redes de escola ou empresa, mudar configurações de rede pode contrariar as regras de uso; nesse caso, o caminho certo é falar com o administrador. O programa avisa sobre isso antes de alterar o DNS.

## Licença

[MIT](LICENSE)
