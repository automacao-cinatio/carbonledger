# Coleta e evidências

Como a coleta funciona, o formato da planilha e o que fazer quando um envio é recusado.

## Fluxo

1. Administrador ou gestor abre o inventário da empresa em foco (Coleta, Abrir inventário): ano e abordagem de consolidação. O GWP é AR5, 100 anos.
2. Administrador, gestor ou responsável cadastra as fontes de emissão de cada unidade: categoria (escopos 1 e 2), unidade de medida (pint: L, m**3, kg, t, kWh, MWh, GJ...), periodicidade (mensal ou anual) e se a fonte é material. Fonte fora da materialidade exige justificativa. Energia elétrica é sempre mensal.
3. Qualquer perfil lança o dado de cada período, pelo formulário ou pela planilha. Cada envio vira uma versão nova; nada é sobrescrito.
4. Outra pessoa (responsável, gestor ou administrador) aprova ou devolve com motivo. Quem lançou não decide: o banco recusa (erro CL014). Só a versão mais recente é decidida (CL015).
5. Qualquer perfil anexa evidências à fonte, para um período ou para todos.

O progresso da faixa de estado conta só fontes ativas e materiais: "Coleta" são os períodos com dado aprovado; "Evidências" são os períodos com pelo menos uma evidência (do período ou de todos os períodos).

## Planilha de coleta (CSV)

Baixe em Inventário, Planilha de coleta. Colunas:

| Coluna | Lida na importação | Conteúdo |
| --- | --- | --- |
| fonte_id | sim | Identificador da fonte. Não mude. |
| unidade, categoria, descricao, nome_periodo | não | Só para se achar. |
| periodo | sim | `01` a `12` (fonte mensal) ou `ano` (fonte anual). Não mude. |
| quantidade | sim | Número em pt-BR (`1234,5` ou `1.234,5`). Vazio: linha ignorada. |
| unidade_medida | sim | Unidade da mesma grandeza da fonte (L ou m**3; kWh ou MWh). |
| data_compra | sim | `dd/mm/aaaa`, opcional. Para combustível, define a mistura. |
| observacao | sim | Opcional, até 500 caracteres. |

Regras:

- UTF-8 (como o modelo) ou Windows-1252 (como o Excel salva "CSV separado por vírgulas"); separador `;`.
- Tudo ou nada: com qualquer erro, nada é gravado e a tela lista até 50 problemas com linha e coluna.
- Linha igual ao dado atual não cria versão. Linha alterada cria versão nova, que volta a pedir aprovação, mesmo se o dado anterior estava aprovado.
- O arquivo importado fica guardado no S3 (pasta planilhas) com o SHA-256 em planilha_importacao.
- No modelo, células que começam com `=`, `+`, `-` ou `@` saem com apóstrofo na frente (contra injeção de fórmula); a importação tira o apóstrofo.

## Evidências

Ordem de checagem: nome, tamanho (UPLOAD_LIMITE_MB, padrão 20), tipo pelo conteúdo, ClamAV, S3, banco.

- Aceitos: PDF, PNG, JPEG, XLSX, DOCX, CSV, TXT e XML. A extensão precisa bater com o conteúdo.
- Recusados: .xlsm, .xlsb, .docm e qualquer pacote com vbaProject.bin (macro); .xls e .doc antigos; .zip; executáveis.
- ClamAV fora do ar ou com resposta inesperada: o envio é recusado ("Tente de novo em alguns minutos"). Nada é guardado sem veredito.
- O objeto vai para `tenants/<tenant>/empresas/<empresa>/evidencias/<id>`, sem o nome do arquivo (que pode ter dado pessoal e fica só no banco), com retenção Object Lock (EVIDENCIAS_MODO_RETENCAO e EVIDENCIAS_RETENCAO_DIAS) e checksum SHA-256 conferido pelo S3.
- O download confere o SHA-256 de novo; se não bater, responde erro em vez de entregar o arquivo.
- Evidência não se apaga nem se troca. Arquivo errado: envie o certo; o errado fica no histórico.

## Problemas comuns

| Sintoma | Causa e saída |
| --- | --- |
| "Não foi possível verificar o arquivo agora" | ClamAV parado ou atualizando assinaturas. `docker compose ps clamav`; espere ficar saudável (o primeiro start leva minutos). |
| "Outra pessoa gravou este período agora mesmo" | Duas gravações simultâneas da mesma versão. Atualize a página e confira. |
| "Quem lançou o dado não pode aprová-lo" | Segregação de funções. Peça a outra pessoa com perfil de responsável, gestor ou administrador. |
| Planilha com acento estranho na tela | O arquivo não está em UTF-8 nem em Windows-1252. Salve como "CSV UTF-8". |
| Erro 500 no download de evidência | O SHA-256 do objeto não bate com o registro. Trate como incidente de integridade: não reenvie, abra o registro da evidência e compare com o S3. |
