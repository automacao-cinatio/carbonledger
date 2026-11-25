# Demonstração local

`make seed` (com a pilha de pé: `make up` e `make migrate`) cria um cliente de demonstração completo, percorrendo o app como pessoas de verdade:

1. A curadoria importa e aprova a referência FICTÍCIA de tests/dados_ficticios/demonstracao (códigos `demo.*`, GWP `DEMO_FICTICIO_100`). Na segunda execução, a referência já está lá e é reaproveitada.
2. Um cliente novo ("Demonstracao FICTICIA" com data e hora) e a administradora, que entra pelo provedor de desenvolvimento.
3. Grupo, duas empresas e unidades.
4. Inventário 2025 da Metalúrgica Exemplo: caldeira a diesel (mensal, com mistura), energia elétrica (mensal) e viagens aéreas (anual), com evidências em PDF passando pelo ClamAV e pelo S3.
5. Um gestor convidado (convite lido do Mailpit) aprova os dados, porque quem lança não aprova.
6. Cálculo, conferência e fechamento.
7. Certificado A1 autoassinado com o CNPJ da empresa (só desenvolvimento aceita sem cadeia ICP-Brasil) e o relatório assinado.
8. Transportes Exemplo com a coleta pela metade, para o painel mostrar progresso.

Para entrar depois: "Entrar" com o provedor de desenvolvimento, usuário `dona-demonstracao` (e-mail dona@demonstracao.example) ou `gestor-demonstracao` (gestor@demonstracao.example).

Números esperados do inventário fechado (conferidos à mão): escopo 1 = 34,134 tCO₂e, escopo 2 = 47,308 tCO₂e, escopo 3 = 7,245 tCO₂e e CO₂ biogênico = 5,531 t.

## Cuidados

- Tudo é fictício, inclusive os fatores. Nunca rode em produção: o script recusa se a API estiver com AMBIENTE=producao.
- O único atalho fora do app: o inventário da demonstração passa a usar o GWP `DEMO_FICTICIO_100`, direto no banco local, porque a tela sempre usa AR5_100.
- Cada execução cria um cliente novo; a referência fictícia fica no banco local. Para limpar, recrie os volumes da pilha (`docker compose down -v` e `make up`), sabendo que isso apaga todos os dados locais.
