# Demonstração local

`make seed` (com a pilha de pé: `make up` e `make migrate`) cria um cliente de demonstração completo, percorrendo o app como pessoas de verdade:

1. A curadoria importa e aprova a referência FICTÍCIA de tests/dados_ficticios/demonstracao (códigos `demo.*`, GWP `DEMO_FICTICIO_100`). Na segunda execução, a referência já está lá e é reaproveitada.
2. Um cliente novo ("Demonstracao FICTICIA" com data e hora) e a administradora, que entra pelo provedor de desenvolvimento.
3. Grupo, duas empresas e unidades.
4. Inventário 2025 da Metalúrgica Exemplo: caldeira a diesel (mensal, com mistura), energia elétrica (mensal) e viagens aéreas (anual), com evidências em PDF passando pelo ClamAV e pelo S3.
5. Um gestor convidado (convite lido do Mailpit) aprova os dados, porque quem lança não aprova.
6. Cálculo, conferência e fechamento.
7. Certificado A1 autoassinado com o CNPJ da empresa (só desenvolvimento aceita sem cadeia ICP-Brasil) e o relatório assinado, gerado pelo worker: o script espera até 90 segundos e para com aviso se o worker não estiver de pé.
8. Transportes Exemplo com a coleta pela metade, para o painel mostrar progresso.
9. Uma responsável da Transportes Exemplo (convite aceito) e uma campanha de coleta aberta para ela, com prazo de 15 dias.
10. Inventário do ano corrente da Metalúrgica em andamento: caldeira e energia até o mês passado, com menos diesel que em 2025, aprovados e calculados (preliminar). O painel e a meta mostram a evolução.
11. Meta "Reduzir 30% das emissões até 2030" (escopos 1 e 2, base 2025) com duas ações, uma em andamento.
12. Verificador com acesso de 60 dias ao inventário fechado, já aceito (link lido do Mailpit).
13. Verificação de integridade pedida na tela e feita pelo worker; o script espera até 120 segundos.

Para dar acesso de administradora a uma pessoa real no cliente da demonstração, passe o e-mail: `uv run python scripts/demonstracao.py --convidar pessoa@empresa.com.br`. O script imprime o link do convite (o e-mail também chega ao Mailpit); a pessoa abre o link e entra com aquele e-mail. O e-mail não fica gravado no projeto.

Para entrar depois: "Entrar" com o provedor de desenvolvimento, usuário `dona-demonstracao` (e-mail dona@demonstracao.example) ou `gestor-demonstracao` (gestor@demonstracao.example). A responsável (responsavel@demonstracao.example) e o verificador (verificador@demonstracao.example) entram pelo e-mail, na tela de login do ambiente local.

Números esperados do inventário fechado (conferidos à mão): escopo 1 = 34,134 tCO₂e, escopo 2 = 47,308 tCO₂e, escopo 3 = 7,245 tCO₂e e CO₂ biogênico = 5,531 t.

## Cuidados

- Tudo é fictício, inclusive os fatores. Nunca rode em produção: o script recusa se a API estiver com AMBIENTE=producao.
- O único atalho fora do app: o inventário da demonstração passa a usar o GWP `DEMO_FICTICIO_100`, direto no banco local, porque a tela sempre usa AR5_100.
- Cada execução cria um cliente novo; a referência fictícia fica no banco local. Para limpar, recrie os volumes da pilha (`docker compose down -v` e `make up`), sabendo que isso apaga todos os dados locais.
