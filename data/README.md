# Dados (insumos versionados)

Insumos que os prompts leem; nenhum número é inventado, e sem eles o Claude Code para e pergunta.

| Pasta ou arquivo | Conteúdo esperado | Usado em |
| --- | --- | --- |
| data/templates/ | Ferramenta oficial do Programa Brasileiro GHG Protocol, Ciclo 2027 | P13 |
| data/paridade/<caso>/ | 3 a 5 casos de paridade: entradas e saídas calculadas na ferramenta oficial | P10, P13, P14, P17 |
| data/fatores/ | Fatores de emissão em CSV, com fonte e vigência | P07, P09 |
| data/fatores/sin/ | Fator mensal do SIN (MCTI, SIRENE) | P10 |
| data/gwp/ar5.csv | GWP AR5 de 100 anos (IPCC) | P07 |
| data/misturas.csv | Misturas de combustível com vigência (CNPE e MME) | P07, P09 |
| data/xsd/ | XSD da NF-e 4.00 e do CT-e 4.00 | P15 |
| data/templates/coleta/ | Planilhas de coleta e regra de materialidade (oficiais da CINATIO, quando houver) | P08 |

## Formato dos arquivos de referência (GWP, fatores e misturas)

Regras comuns:

- CSV em UTF-8 (pode ter BOM, como o Excel salva), separador `;`.
- Números no padrão brasileiro: `1.234,56`; números muito pequenos também em notação científica, `1,5E-04`.
- Datas em `dd/mm/aaaa` ou `aaaa-mm-dd`.
- Marcações com `sim` ou `nao`.
- Linhas começando com `#` são comentários.
- Toda linha leva a fonte (documento, edição, tabela). Sem fonte, a linha é recusada.
- Códigos em minúsculas, números, `_` e pontos (`combustao_estacionaria.oleo_diesel`).

Os modelos em data/modelos/ trazem só o cabeçalho. Nenhum número entra em data/ sem vir da fonte oficial.

### GWP (data/gwp/)

| Coluna | Conteúdo |
| --- | --- |
| conjunto | Identificador do conjunto, em maiúsculas: `AR5_100` (AR5, horizonte de 100 anos) |
| gas | Gás: `CO2`, `CH4`, `N2O`, `HFC-134a`, `SF6`... |
| valor | GWP (maior que zero) |
| fonte | Documento de origem (por exemplo, IPCC AR5, tabela e página) |

### Fatores (data/fatores/)

| Coluna | Conteúdo |
| --- | --- |
| codigo | Atividade a que o fator se aplica |
| descricao | Texto para a tela |
| categoria | Categoria do GHG Protocol (`escopo1.combustao_estacionaria`) |
| gas | Gás emitido |
| biogenico | `sim` para CO2 biogênico (relatado fora dos escopos) |
| valor | Fator (zero ou mais) |
| unidade | Massa do gás por unidade da atividade: `kg/L`, `t/MWh`, `kg/GJ`, `g/km` |
| vigencia_inicio | Primeiro dia em que o fator vale |
| vigencia_fim | Último dia (vazio: sem fim) |
| fonte | Documento de origem |
| referencia | Opcional: tabela, link ou número do documento |

No mesmo arquivo, o mesmo código, gás e marcação de biogênico não podem ter vigências sobrepostas.

### Misturas (data/misturas.csv)

| Coluna | Conteúdo |
| --- | --- |
| combustivel | Combustível vendido (`diesel_comercial`) |
| componente | Componente da mistura (`biodiesel`, `diesel_fossil`) |
| percentual | Participação de 0 a 100 |
| vigencia_inicio | Primeiro dia em que a mistura vale |
| vigencia_fim | Último dia (vazio: sem fim) |
| fonte | Norma de origem (resolução do CNPE, portaria do MME) |

A soma dos componentes de um combustível com a mesma vigência não passa de 100. A mistura usada no cálculo é a da data da compra.

## Como entra no sistema

Nada em data/ vale sozinho. A curadoria importa cada arquivo como um lote, confere e aprova; só então o cálculo usa. O passo a passo está em docs/runbooks/curadoria-de-fatores.md.

## Coleta (data/templates/coleta/)

A pasta continua vazia. Enquanto as planilhas oficiais e a regra de materialidade não chegam:

- a planilha de coleta é gerada pelo próprio sistema, por inventário (CSV com `;`, uma linha por fonte e período); o formato está em docs/runbooks/coleta-e-evidencias.md;
- a materialidade é decidida fonte a fonte: cada fonte entra ou fica de fora com justificativa registrada. Nenhum limite percentual foi definido.

Quando os arquivos oficiais chegarem, a planilha e a regra passam a segui-los.
