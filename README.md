# BenaExt - Repositorio Oficial

Bem-vindo ao **BenaExt**, um software local para comparacao, auditoria e consolidacao de rotulagens de imagens biometricas.

O BenaExt roda no Windows com uma interface grafica local baseada em `pywebview`. A ferramenta permite carregar um JSON original, uma pasta com JSONs de usuarios/anotadores e, opcionalmente, um ZIP com as imagens para navegar visualmente pelos casos avaliados.

Este repositorio reune:

- **Codigo fonte em Python** para execucao, manutencao e evolucao do projeto.
- **Aplicativo Windows** empacotado para uso direto.
- **Recursos auxiliares** usados pela interface e arquivos de apoio para testes locais.

---

## Sobre o Projeto

O BenaExt foi desenvolvido para apoiar a etapa de revisao de bases rotuladas, permitindo comparar respostas de diferentes usuarios com um gabarito ou arquivo original.

A ferramenta permite:

- carregar um JSON original/gabarito;
- carregar uma pasta com multiplos JSONs de usuarios;
- carregar um ZIP de imagens para visualizacao integrada;
- calcular metricas por usuario, por erro e por imagem;
- identificar divergencias entre usuarios e gabarito;
- medir consenso por rotulos;
- visualizar matrizes, rankings e graficos;
- exportar resultados em CSV, JSON, HTML, PDF e imagens de graficos;
- executar localmente, sem envio de dados para servidor externo.

---

## Compatibilidade

| Sistema Operacional | Execucao principal | Executavel |
|---------------------|-------------------|------------|
| Windows 10/11       | `BenaExt.py`      | `BenaExt.exe` |

> Este projeto esta documentado apenas para Windows.

---

## Estrutura do Repositorio

```text
BenaExt/
|-- Aplicativo/
|   |-- BenaExt.exe
|   |-- README.md
|   `-- Leia-me.txt
|-- Codigo Fonte/
|   |-- BenaExt.py
|   |-- BenaExt.ico
|   |-- requirements.txt
|   |-- README.md
|   |-- Complementos/
|   `-- Parte Unificada com ajustes/
|-- .gitattributes
|-- .gitignore
|-- LICENSE
`-- README.md
```

---

## Componentes

### 1. Codigo Fonte

Contem a implementacao principal do BenaExt.

Arquivos principais:

- `Codigo Fonte/BenaExt.py`: aplicacao principal.
- `Codigo Fonte/requirements.txt`: dependencias Python para execucao pelo codigo fonte.
- `Codigo Fonte/BenaExt.ico`: icone do projeto.
- `Codigo Fonte/Complementos/`: imagens e audios auxiliares usados pela interface.
- `Codigo Fonte/Parte Unificada com ajustes/`: arquivos locais de exemplo/apoio para testes.

Manual especifico:

- `Codigo Fonte/README.md`

### 2. Aplicativo

Contem a versao empacotada para Windows.

Arquivos principais:

- `Aplicativo/BenaExt.exe`: executavel principal.
- `Aplicativo/README.md`: manual resumido.
- `Aplicativo/Leia-me.txt`: instrucoes rapidas.

> Distribuicao `onefile`: o executavel inclui os recursos e bibliotecas necessarios.

## Como Executar pelo Aplicativo

1. Abra a pasta `Aplicativo`.
2. Execute `BenaExt.exe`.

Se o Windows SmartScreen bloquear a abertura, clique em `Mais informacoes` e depois em `Executar assim mesmo`.

---

## Como Executar pelo Codigo Fonte

### 1. Entrar na pasta do codigo

```powershell
cd "C:\Users\mathe\Downloads\BenaExt\Codigo Fonte"
```

### 2. Instalar dependencias

```powershell
python -m pip install -r requirements.txt
```

### 3. Executar o BenaExt

```powershell
python BenaExt.py
```

O aplicativo abre em tela cheia.

---

## Como Usar

1. Selecione o JSON original/gabarito.
2. Selecione a pasta com os JSONs dos usuarios.
3. Selecione o ZIP de imagens, se quiser visualizar as imagens junto das metricas.
4. Navegue pelas imagens, filtros, graficos e tabelas.
5. Exporte os resultados conforme necessario.

---

## Entradas

O BenaExt aceita:

- arquivo `.json` original/gabarito;
- pasta com arquivos `.json` de usuarios;
- arquivo `.zip` contendo imagens;
- imagens nos formatos `.png`, `.jpg`, `.jpeg`, `.bmp`, `.webp`, `.tif` e `.tiff` dentro do ZIP.

---

## Saida de Dados

O BenaExt pode exportar:

| Saida | Conteudo |
|-------|----------|
| `BenaExt Resultados.csv` | Tabela consolidada por imagem/usuario. |
| `BenaExt Comparacao.json` | Comparacao completa em formato estruturado. |
| `benaext_relatorio.html` | Relatorio HTML autocontido. |
| `BenaExt Relatorio.pdf` | Relatorio PDF com resumo, tabelas e graficos. |
| Graficos `.png` | Imagens dos graficos exibidos na interface. |

---

## Observacoes para GitHub

- O arquivo `Codigo Fonte/Parte Unificada com ajustes/imagens_unificadas.zip` e grande demais para GitHub comum e esta ignorado em `.gitignore`.
- O executavel `Aplicativo/BenaExt.exe` e grande porque inclui Qt/WebEngine em modo `onefile`; para versionar no GitHub, use Git LFS ou publique o executavel em Releases.
- O arquivo local de sessao `BenaExt Log.state` tambem fica ignorado.

---

## Requisitos Tecnicos

- Sistema operacional: Windows 10 ou Windows 11.
- Python: 3.10 ou superior, para execucao pelo codigo fonte.
- Microsoft Edge WebView2 Runtime, para o executavel Windows.
- Dependencias principais para codigo fonte:
  - `pywebview[qt]`
  - `Pillow`

---

## Suporte e Contato

Para duvidas, suporte tecnico ou colaboracoes:

- Matheus Augusto - [matheusaugustooliveira@alunos.utfpr.edu.br](mailto:matheusaugustooliveira@alunos.utfpr.edu.br)

---

## Agradecimentos

Este projeto foi desenvolvido na UTFPR com apoio do projeto de pesquisa em biometria neonatal.

---

## Licenca

Distribuicao autorizada conforme os termos do arquivo `LICENSE`.
