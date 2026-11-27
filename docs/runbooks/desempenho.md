# Desempenho com volume

Como medir as telas e as ações pesadas com um cliente grande, na pilha local. Só desenvolvimento: o script recusa rodar com a API em produção, e toda a referência usada é FICTÍCIA (a mesma da demonstração).

## Gerar o cliente grande

Com a pilha no ar (`make up`, Docker Desktop iniciado):

```
uv run python scripts/volume.py gerar --empresas 12 --fontes 30 --anos 2
```

O script percorre o app pelo HTTP, como a demonstração: cria um cliente novo, um grupo, as empresas com três unidades cada, um inventário por ano, as fontes (mensais e anuais), lança os dados pela importação da planilha (um envio por inventário) e um gestor aprova cada dado, em paralelo. O ano mais antigo de cada empresa sai calculado, conferido e fechado, com uma evidência por fonte; o mais recente fica calculado e aberto. Com os valores acima: 24 inventários e 6.792 dados, em uns 8 minutos.

No fim, ele imprime o e-mail da administradora do cliente grande. Cada execução cria um cliente novo.

Cuidados:

- Use `http://127.0.0.1:8000` (o padrão do script). No Windows, `localhost` soma uns 200 ms a cada requisição (tenta IPv6 antes) e distorce tudo.
- O app limita a 20 inícios de login por IP a cada 10 minutos. O script abre as sessões uma vez só; se rodar várias vezes seguidas e receber 429, espere a janela.
- Não edite arquivos em `app/` durante a geração: a API do dev recarrega e derruba as requisições em andamento.

## Medir

```
uv run python scripts/volume.py medir --email <e-mail impresso pelo gerar>
```

Mostra a mediana e o máximo de cada tela (5 leituras) e das ações pesadas: calcular o inventário aberto, importar a planilha sem mudança e a verificação de integridade (o clique e o tempo até o worker concluir).

O log de cada requisição traz `duracao_ms`, o tempo no servidor até a resposta ficar pronta. Para ver o que pesa numa tela: `make logs` e filtrar pelo caminho. Para as consultas, ligar no Postgres local `ALTER SYSTEM SET log_min_duration_statement = 100` e `SELECT pg_reload_conf()`, e desligar depois com `ALTER SYSTEM RESET log_min_duration_statement`.

## Referência (2026-11-27, pilha local no Windows)

Cliente com 12 empresas, 30 fontes cada, dois anos (6.792 dados aprovados, 12 inventários fechados). Mediana em ms, medida do cliente (inclui a rede do Docker Desktop).

| Tela ou ação | Antes | Depois |
| --- | ---: | ---: |
| Painel | 428 | 280 |
| Fechamento (inventário aberto) | 736 | 455 |
| Relatórios | 777 | 117 |
| Importar planilha sem mudança (283 linhas) | 726 | 150 |
| Calcular inventário (283 dados) | 529 | 451 |
| Verificar integridade (clique) | 11.514 | 66 |
| Verificação no worker (até concluir) | (na requisição) | 12.158 |

As demais telas ficam entre 100 e 200 ms. Parte do tempo de base (perto de 100 ms) é do ambiente de dev: recarga automática do uvicorn e templates lidos de volume montado no Windows, que o Jinja confere a cada uso; na imagem de produção esse custo some.

O que ainda cresce com o uso: a verificação de integridade (linear em eventos, cálculos e arquivos; roda no worker) e o Fechamento de inventário aberto, que monta a entrada e refaz a conta uma vez para a conferência.
