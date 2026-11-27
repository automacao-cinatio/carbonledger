# Login e acessos

## Como funciona

- Entrada só por OIDC: Google, Microsoft (contas corporativas ou de escola) e, fora de produção, o provedor simulado.
- Ninguém se cadastra sozinho. A pessoa entra por um convite, que vale só para o e-mail convidado, uma vez, por 7 dias (CONVITE_VALIDADE_DIAS).
- Depois do primeiro acesso, a pessoa entra por /entrar/google ou /entrar/microsoft.
- Perfis: administrador (usuários e todas as empresas), gestor (todas as empresas), responsável e colaborador (só as empresas atribuídas).
- A sessão expira após 2 horas sem uso (SESSAO_INATIVIDADE_MIN) e, de qualquer forma, após 12 horas (SESSAO_MAXIMA_H).

## Criar um cliente novo (operação CINATIO)

```
docker compose exec api python -m app.cli criar-tenant --nome "Cliente S.A." --email admin@cliente.com.br
```

O comando cria o tenant e mostra um link de convite por provedor habilitado. Repasse o link só para a pessoa convidada. Depois disso, ela convida o restante da equipe pelo portal.

## Desenvolvimento

O serviço mock-oidc simula o provedor, com a tela de login própria de infra/mock-oidc/login.html (nas cores da plataforma). Para entrar:

1. Crie um tenant com o comando acima e abra o link do provedor "dev" no navegador.
2. Na tela "Entrar", digite o e-mail do convite (e, se quiser, o nome) e clique em Entrar. O identificador da pessoa é o próprio e-mail; em "Opções avançadas" dá para usar outro (os da demonstração e do teste de volume usam `dona-demonstracao`, `dona-<data>` e assim por diante). A tela lembra o último acesso no navegador.
3. Os botões "Administradora da demonstração" e "Gestor da demonstração" entram direto com as pessoas fictícias de `make seed`.
4. O e-mail precisa ser o mesmo do convite. Os e-mails de convite chegam no Mailpit (http://localhost:8025).

Por baixo, a tela envia ao simulador os mesmos campos da tela original dele (username e claims); nada muda no fluxo OIDC do app. Mudou a tela? Recrie o serviço: `docker compose up -d --force-recreate mock-oidc`.

## Google

1. No Google Cloud Console, crie um cliente OAuth do tipo "Aplicativo da Web".
2. URI de redirecionamento autorizado: `<APP_URL_BASE>/entrar/google/retorno`.
3. Preencha OIDC_GOOGLE_CLIENT_ID e OIDC_GOOGLE_CLIENT_SECRET no gerenciador de segredos.

## Microsoft (Entra ID)

1. Registre um aplicativo com contas "em qualquer diretório organizacional" (multilocatário, sem contas pessoais).
2. URI de redirecionamento (Web): `<APP_URL_BASE>/entrar/microsoft/retorno`.
3. Crie um segredo do cliente.
4. Em "Configuração de token", adicione as declarações opcionais `email` e `xms_edov` ao token de ID. Sem a `xms_edov`, o e-mail não é considerado verificado e o convite não é aceito.
5. Preencha OIDC_MICROSOFT_CLIENT_ID e OIDC_MICROSOFT_CLIENT_SECRET no gerenciador de segredos.

## Quando alguém não consegue entrar

A tela responde "Não encontrado" para todo motivo, de propósito. O motivo fica no log (evento login_falhou, campo etapa):

| etapa | Causa provável |
| --- | --- |
| state | O login demorou mais de 10 minutos ou começou em outro navegador. Basta tentar de novo. |
| token | Relógio, segredo ou configuração do provedor. |
| convite | Convite expirado, revogado, já usado, ou e-mail do login diferente do convidado. |
| sem_vinculo | A pessoa nunca aceitou um convite. Peça ao administrador do cliente um convite novo. |
