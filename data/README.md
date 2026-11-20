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
| data/templates/coleta/ | Planilhas de coleta e regra de materialidade | P08 |