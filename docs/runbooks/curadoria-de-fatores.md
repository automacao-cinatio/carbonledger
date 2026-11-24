# Curadoria de fatores, GWP e misturas

Fatores, GWP e misturas de combustível são a referência de todo cálculo. Ninguém digita esses números no portal: eles entram por arquivo, viram um lote e só valem depois de aprovados por uma pessoa da CINATIO.

## Princípios

- Nenhum número inventado: todo valor vem da fonte oficial, citada na coluna fonte.
- Nada muda nem some: corrigir um valor é importar uma versão nova e aprová-la. A versão antiga continua guardada, e um inventário calculado antes da correção refaz exatamente a mesma conta.
- Quem importa e quem aprova ficam registrados, com data e hora.

## Passo a passo

1. Prepare o arquivo a partir do modelo em data/modelos/, seguindo o formato de data/README.md. Coloque em data/gwp/, data/fatores/ ou data/misturas.csv.
2. Importe (o arquivo inteiro é validado; se houver erro, nada é gravado e cada problema aparece com linha e coluna):

   ```
   docker compose run --rm curadoria fatores importar --tipo fatores --por "Seu nome" data/fatores/arquivo.csv
   ```

   Tipos: `gwp`, `fatores` e `misturas`. Importar de novo o mesmo arquivo não faz nada.

3. Confira o lote: `docker compose run --rm curadoria fatores lotes`. Linhas iguais a versões já aprovadas não entram de novo; o resumo da importação mostra quantas.
4. Uma segunda pessoa confere os valores contra a fonte e decide:

   ```
   docker compose run --rm curadoria fatores aprovar <lote> --responsavel "Nome de quem conferiu"
   docker compose run --rm curadoria fatores rejeitar <lote> --responsavel "Nome" --motivo "o que está errado"
   ```

   A decisão é única: um lote aprovado ou rejeitado não muda. Para corrigir, importe um arquivo novo.

## Como o cálculo escolhe

Para uma atividade numa data, vale a versão aprovada por último cuja vigência cobre a data. O cálculo guarda o instante (do relógio do banco) em que leu a referência; com ele, a mesma conta é refeita depois com os mesmos fatores e o mesmo hash, mesmo que versões novas tenham sido aprovadas.

A mistura de combustível usada é a da data da compra.

## Acesso

- O serviço curadoria conecta com o papel app_curadoria (CURADORIA_DATABASE_URL), o único que insere na referência.
- A API e os workers só leem. Nenhum papel altera ou apaga: uma trigger recusa até o superusuário.
- Em produção, a senha do app_curadoria fica no gerenciador de segredos e o job de curadoria roda à parte da aplicação.
