# Dados fictícios

Arquivos só para os testes. Os valores são inventados e não representam nenhum fator,
GWP ou mistura real. Nunca copie daqui para data/: a referência real vem das fontes
oficiais e passa pela curadoria (docs/runbooks/curadoria-de-fatores.md).

## demonstracao/

Referência fictícia da demonstração local (`make seed`, scripts/demonstracao.py), com códigos
`demo.*` e o conjunto de GWP `DEMO_FICTICIO_100`, para nunca se confundir com a referência
oficial (o inventário de verdade usa AR5_100). Entra só no banco local, pela curadoria, e cada
linha diz FICTICIO na fonte e na descrição. Nunca importe estes arquivos em produção.
