# BenaExt - Codigo Fonte

Esta pasta contem o codigo Python, recursos auxiliares e arquivos locais de apoio usados pelo BenaExt.

## Requisitos

- Windows 10 ou Windows 11.
- Python 3.10 ou superior.
- Dependencias listadas em `requirements.txt`.

## Instalar dependencias

```powershell
python -m pip install -r requirements.txt
```

## Executar

```powershell
python BenaExt.py
```

Execute o comando a partir desta pasta para manter os caminhos dos recursos organizados.

## Conteudo

- `BenaExt.py`: arquivo principal do aplicativo.
- `BenaExt.ico`: icone do projeto.
- `requirements.txt`: dependencias Python.
- `Complementos/`: imagens e audios auxiliares usados pela interface.
- `Parte Unificada com ajustes/`: arquivos locais de exemplo, resultados e limpeza usados como apoio.

## Fluxo basico

1. Abra o BenaExt.
2. Carregue o JSON original/gabarito.
3. Carregue a pasta com os JSONs dos usuarios.
4. Carregue o ZIP de imagens, se quiser visualizar as imagens.
5. Analise consenso, divergencias, metricas e graficos.
6. Exporte CSV, JSON, HTML, PDF ou graficos.

## Dados locais

O arquivo `BenaExt Log.state` pode ser criado automaticamente para restaurar a ultima sessao. Ele nao deve ser versionado.

O arquivo `Parte Unificada com ajustes/imagens_unificadas.zip` e grande demais para GitHub comum e esta ignorado pelo `.gitignore` da raiz. Mantenha esse ZIP localmente ou use Git LFS caso precise versiona-lo.

## Executavel

A versao pronta para uso fica em `..\Aplicativo\BenaExt.exe`.
