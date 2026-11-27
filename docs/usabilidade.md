# Usabilidade

Registro das revisões de usabilidade: o que foi olhado, o que se achou, o que mudou e o que ficou para depois. As regras de base estão na seção UI e UX do CLAUDE.md; a acessibilidade automática (axe-core, WCAG 2.2 AA) roda em todas as telas no CI.

## 2026-11-25: revisão sobre a demonstração

Como foi feita: a administradora da demonstração (`make seed`) percorreu, num navegador de 1440 px, Início, Organização, Coleta, Resultados, Fechamento, Relatórios, Verificação, Metas, Painel, Usuários, Cobrança, Integridade e Meus dados. Em cada tela, conferência automática (um título h1, próximo passo, seletor de empresa, faixa de estado do inventário, campos com rótulo, números alinhados à direita) e leitura das capturas.

Conferência automática: todas as telas têm h1, próximo passo, seletor de empresa e faixa de estado; nenhum número à esquerda nas tabelas; todos os campos têm rótulo (as caixas de marcar de empresas, em Usuários, ficam dentro do próprio rótulo).

### Achados e correções

| Tela | Achado | Correção |
| --- | --- | --- |
| Início | A jornada dizia "Inventário 2025 aberto" com o inventário fechado | O detalhe da coleta segue o estado: em coleta ou fechado |
| Início | A jornada não tinha a etapa do relatório assinado (P19), que vem antes do verificador | Etapa "Relatório assinado" entre Fechamento e Verificação |
| Início | O resumo mostrava só os escopos 1 e 2 | Escopo 3 incluído |
| Menu | Verificação vinha antes de Relatórios, contra a ordem do trabalho (o relatório vai para o verificador) | Relatórios antes de Verificação |
| Coleta | Com o inventário fechado, o próximo passo dizia "Continue a coleta" | Diz que está fechado e leva aos relatórios |
| Coleta | O formulário sugeria 2025, ano que já tinha inventário | Sugere o ano anterior mais recente sem inventário |
| Resultados | Com o inventário fechado, o próximo passo ainda era fechar; sem link | Diz que está fechado e leva aos relatórios; em coleta, leva ao fechamento |
| Fechamento | Depois de fechar, mandava direto para a verificação, sem link | Sem relatório, leva aos relatórios; com relatório, à verificação |
| Painel | "Ltda.." (ponto do nome mais o da frase) | Ponto do nome tirado |
| Painel | O link vai para a coleta da empresa selecionada, não da citada | O texto diz para escolher a empresa no seletor do topo |
| Painel e gráficos | Escopos 1, 2 e 3 em três azuis quase iguais | Paleta própria de séries no tema, validada (ver abaixo), e uma linha da cor do fundo entre segmentos empilhados |

### Paleta dos gráficos

`--cl-serie-1` a `--cl-serie-5` em tema.css, em ordem fixa: #4338ca, #0f9f8f, #c2410c, #a21caf, #65a30d. Passou no validador de paleta categórica (luminosidade e croma na faixa, pior par adjacente com ΔE 14,4 para daltonismo e 24,8 para visão normal, contraste de pelo menos 3:1 sobre o fundo). A cor nunca vem sozinha: todo gráfico tem legenda e a tabela equivalente.

### Ficou para depois

- Seletor de arquivo: o texto do botão segue o idioma do navegador da pessoa (em inglês só no navegador de teste sem idioma). Resolvido em 2026-11-27 (seção abaixo).

## 2026-11-25: pendências da revisão

| Tela | Achado | Correção |
| --- | --- | --- |
| Resultados | O cartão "Emissões por categoria" repetia o título do gráfico logo abaixo | A legenda do gráfico diz o que ele mostra: total de cada categoria, da maior para a menor, em tCO₂e |
| Topo | Nome de cliente longo quebrava em duas linhas ao lado do nome da pessoa | Nome da pessoa e do cliente em uma linha, com largura máxima; o nome inteiro fica no title |
| Painel | O próximo passo mandava escolher a empresa no seletor do topo antes de ir para a coleta | O botão "Ir para a coleta de (empresa)" põe a empresa citada em foco e abre a coleta (destino aceito só entre as telas do menu) |

## 2026-11-27: seletor de arquivo em português

| Tela | Achado | Correção |
| --- | --- | --- |
| Evidência da fonte, importação da planilha, certificado A1 e /ui | O botão nativo e o "nenhum arquivo" seguem o idioma do navegador ("Choose file", "No file chosen") | Botão "Escolher arquivo" e o nome do arquivo escolhido em português, em qualquer navegador. O campo de verdade continua no teclado, no leitor de tela (nome só do rótulo) e no envio; o foco e o erro aparecem no botão |
