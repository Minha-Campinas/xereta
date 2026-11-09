"""
Robô do Xereta: coleta os dados da API do SAPL da Câmara Municipal de Campinas
e monta a página index.html.

Uso:
  python coletar.py                 coleta tudo na API e gera index.html
  python coletar.py --leg 18        coleta uma legislatura anterior e gera leg-18.html
  python coletar.py --bruto x.json  gera index.html a partir de uma coleta salva
  python coletar.py --refazer-legs  refaz as páginas leg-N.html a partir das coletas salvas (leg-N.json.gz),
                                    para as legislaturas anteriores seguirem o modelo.html atual sem coletar de novo
"""
import base64, collections, csv, glob, gzip, io, json, os, re, sys, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

API = 'https://sapl.campinas.sp.leg.br/api'
FUSO = timezone(timedelta(hours=-3))
HOJE = datetime.now(FUSO).strftime('%Y-%m-%d')
# Planilha de emendas parlamentares (primeira aba, em CSV). Precisa estar com acesso "qualquer pessoa com o link".
EMENDAS_URL = os.environ.get('EMENDAS_URL') or 'https://docs.google.com/spreadsheets/d/1hNq-clox6dtxRbFNdPzJqhWmrXWG_yM_adFk2uj0DQQ/export?format=csv&gid=0'
sessao = requests.Session()
sessao.headers['User-Agent'] = 'Xereta (acompanhamento cidadao da Camara de Campinas)'


# ---------------------------------------------------------------- acesso à API
def get(caminho):
    """Busca um endereço da API com até 5 tentativas."""
    url = caminho if caminho.startswith('http') else API + caminho
    for t in range(5):
        try:
            r = sessao.get(url, timeout=60)
            if r.status_code == 404:
                return None
            if r.ok:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(2 * (t + 1))
    raise RuntimeError('Falha ao acessar ' + url)


def todos(ep, filtros=''):
    """Busca todas as páginas de uma lista, sempre ordenada por id (o=id).
    Sem o=id a API devolve linhas repetidas e pula outras."""
    base = f'{ep}?{filtros}&page_size=100&o=id'
    primeira = get(base + '&page=1')
    paginas = primeira['pagination']['total_pages']
    resto = paralelo(lambda p: get(f'{base}&page={p}')['results'], range(2, paginas + 1), 4)
    return primeira['results'] + [x for pg in resto for x in pg]


def paralelo(fn, itens, n=6):
    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(fn, list(itens)))


def blocos(lista, n):
    lista = list(lista)
    return [lista[i:i + n] for i in range(0, len(lista), n)]


def log(msg):
    print(datetime.now(FUSO).strftime('%H:%M:%S'), msg, flush=True)


# ---------------------------------------------------------------- coleta
def coletar(leg_num=None):
    R = {'gerado_em': datetime.now(timezone.utc).isoformat()}

    log('legislatura e parlamentares')
    legs = todos('/parlamentares/legislatura/')
    if leg_num:
        leg = next(l for l in legs if l['numero'] == leg_num)
    else:
        leg = next(l for l in legs if l['data_inicio'] <= HOJE <= l['data_fim'])
    INICIO, FIM = leg['data_inicio'], leg['data_fim']
    REF = min(HOJE, FIM)  # data de referência: hoje, ou o último dia de uma legislatura já encerrada
    R['hoje'] = REF
    R['legislatura'] = {'id': leg['id'], 'numero': leg['numero'], 'inicio': INICIO, 'fim': FIM, 'atual': FIM >= HOJE}
    R['parlamentares'] = [dict(id=x['id'], nome=x['nome_parlamentar'], completo=x['nome_completo'], ativo=x['ativo'],
                               email=x.get('email') or None, tel=x.get('telefone') or None, prof=x.get('profissao') or None,
                               foto=x.get('fotografia') or None)
                          for x in todos('/parlamentares/parlamentar/')]
    R['partidos'] = [dict(id=x['id'], sigla=x['sigla']) for x in todos('/parlamentares/partido/')]
    R['filiacoes'] = [dict(p=x['parlamentar'], partido=x['partido'], ini=x['data'], fim=x['data_desfiliacao'])
                      for x in todos('/parlamentares/filiacao/')]
    R['mandatos'] = [dict(p=x['parlamentar'], leg=x['legislatura'], ini=x['data_inicio_mandato'], fim=x['data_fim_mandato'],
                          titular=x['titular']) for x in todos('/parlamentares/mandato/', f'legislatura={leg["id"]}')]

    log('sessões')
    lista = get('/sessao/sessaoplenaria/')
    R['sessoes'] = [dict(id=s['id'], n=s['numero'], d=s['data_inicio'], t=s['tipo'], ata=s.get('upload_ata') or None,
                         pauta=s.get('upload_pauta') or None) for s in lista if INICIO <= s['data_inicio'] <= FIM]

    log('comissões')
    R['comissoes'] = [dict(id=x['id'], nome=x['nome'], sigla=x['sigla'], tipo=x['tipo']) for x in todos('/comissoes/comissao/')]
    R['cargos'] = {x['id']: x['nome'] for x in todos('/comissoes/cargocomissao/')}
    periodo = next((p for p in todos('/comissoes/periodo/') if p['data_inicio'] <= REF <= (p['data_fim'] or '9999')), None)
    comps = todos('/comissoes/composicao/', f'periodo={periodo["id"]}') if periodo else []
    R['composicoes'] = [dict(id=c['id'], comissao=c['comissao']) for c in comps]
    parts = paralelo(lambda c: todos('/comissoes/participacao/', f'composicao={c["id"]}'), comps, 4)
    R['participacoes'] = [dict(comp=c['id'], p=x['parlamentar'], cargo=x['cargo'], titular=x['titular'], fim=x['data_desligamento'])
                          for c, ps in zip(comps, parts) for x in ps]

    log('pauta das sessões')
    od = [x for x in todos('/sessao/ordemdia/', f'data_ordem__gte={INICIO}&data_ordem__lte={FIM}')
          if INICIO <= x['data_ordem'] <= FIM]
    ex = [x for x in todos('/sessao/expedientemateria/', f'data_ordem__gte={INICIO}&data_ordem__lte={FIM}')
          if INICIO <= x['data_ordem'] <= FIM]
    item = {('OD', x['id']): x for x in od}
    item.update({('EX', x['id']): x for x in ex})

    log('votações')
    def rv(campo, ids):
        return todos('/sessao/registrovotacao/', f'{campo}__in=' + ','.join(map(str, ids)))
    regs = [x for b in paralelo(lambda b: rv('ordem', b), blocos([x['id'] for x in od], 80), 4) for x in b]
    regs += [x for b in paralelo(lambda b: rv('expediente', b), blocos([x['id'] for x in ex], 80), 4) for x in b]
    vistos, R['votacoes'] = set(), []
    for v in regs:
        if v['id'] in vistos:
            continue
        vistos.add(v['id'])
        fase = 'OD' if v['ordem'] else 'EX'
        it = item.get((fase, v['ordem'] or v['expediente']))
        if not it:
            continue
        rot = (v['__str__'].split(' - Matéria: ')[1:] or [''])[0].strip()
        R['votacoes'].append(dict(id=v['id'], m=v['materia'], s=it['sessao_plenaria'], fase=fase, turno=it.get('turno'),
                                  res=v['tipo_resultado_votacao'], mat=rot))

    log(f'votos individuais de {len(R["votacoes"])} votações')
    vids = [v['id'] for v in R['votacoes']]
    votos = paralelo(lambda b: todos('/sessao/votoparlamentar/', 'votacao__in=' + ','.join(map(str, b))), blocos(vids, 40), 4)
    R['votos'] = [[x['votacao'], x['parlamentar'], normaliza(x['voto'])] for b in votos for x in b]

    log('presença')
    sids = [s['id'] for s in R['sessoes'] if s['d'] <= REF]
    pres = paralelo(lambda b: todos('/sessao/sessaoplenariapresenca/', 'sessao_plenaria__in=' + ','.join(map(str, b))), blocos(sids, 40), 4)
    R['presenca'] = [[x['sessao_plenaria'], x['parlamentar']] for b in pres for x in b]

    log('reuniões e relatorias das comissões')
    R['reunioes'] = [dict(id=x['id'], c=x['comissao'], n=x['numero'], nome=x['nome'], tema=x['tema'], d=x['data'],
                          hi=x['hora_inicio'], hf=x['hora_fim'], local=x['local_reuniao'], obs=x['observacao'],
                          video=x.get('url_video') or None, audio=x.get('url_audio') or None, pauta=x.get('upload_pauta') or None,
                          ata=x.get('upload_ata') or None, anexo=x.get('upload_anexo') or None)
                     for x in todos('/comissoes/reuniao/', f'data__gte={INICIO}&data__lte={FIM}')
                     if INICIO <= x['data'] <= FIM]
    R['relatorias'] = [dict(m=x['materia'], p=x['parlamentar'], c=x['comissao'], ini=x['data_designacao_relator'],
                            fim=x['data_destituicao_relator'], tfim=x['tipo_fim_relatoria'])
                       for x in todos('/materia/relatoria/', f'data_designacao_relator__gte={INICIO}&data_designacao_relator__lte={FIM}')
                       if x['data_designacao_relator'] and INICIO <= x['data_designacao_relator'] <= FIM]
    R['tipos_fim'] = {x['id']: x.get('descricao') or x['__str__'] for x in todos('/materia/tipofimrelatoria/')}

    log('matérias')
    R['tipos_materia'] = {x['id']: dict(sigla=x['sigla'], desc=x['descricao']) for x in todos('/materia/tipomaterialegislativa/')}
    nominais = {v for v, p, c in R['votos'] if c in 'SNA'}
    # ementa de todas as matérias votadas (não só as nominais), para a busca por palavra funcionar em tudo
    mats = {v['m'] for v in R['votacoes']} | {r['m'] for r in R['relatorias']}
    cache = carregar_cache()
    # todos os projetos apresentados na legislatura (não só os votados), para a aba Projetos
    tipos_proj = [k for k, t in R['tipos_materia'].items() if t['sigla'] in TIPOS_PROJETO]
    R['projetos'] = []
    if tipos_proj:
        log('projetos apresentados')
        for x in todos('/materia/materialegislativa/', f'data_apresentacao__gte={INICIO}&data_apresentacao__lte={FIM}'
                       f'&tipo__in=' + ','.join(map(str, tipos_proj))):
            if not x.get('data_apresentacao') or not INICIO <= x['data_apresentacao'] <= FIM:
                continue
            cache[str(x['id'])] = dict(tipo=x['tipo'], n=x['numero'], ano=x['ano'], ementa=x['ementa'],
                                       autores=x['autores'], texto=x.get('texto_original') or None)
            R['projetos'].append([x['id'], x['data_apresentacao']])
        mats |= {p for p, _ in R['projetos']}
        log(f"{len(R['projetos'])} projetos apresentados")
    faltam = [m for m in mats if str(m) not in cache]
    log(f'{len(mats)} matérias, {len(faltam)} novas')
    for m, x in zip(faltam, paralelo(lambda m: get(f'/materia/materialegislativa/{m}/'), faltam, 6)):
        if x:
            cache[str(m)] = dict(tipo=x['tipo'], n=x['numero'], ano=x['ano'], ementa=x['ementa'],
                                 autores=x['autores'], texto=x.get('texto_original') or None)
    autores = {a for m in mats if str(m) in cache for a in cache[str(m)]['autores']}
    faltam = [a for a in autores if 'a' + str(a) not in cache or 'ap' + str(a) not in cache]
    parl_ids = {p['id'] for p in R['parlamentares']}
    for a, x in zip(faltam, paralelo(lambda a: get(f'/base/autor/{a}/'), faltam, 6)):
        if x:
            cache['a' + str(a)] = x['nome']
            # autor do tipo parlamentar aponta para a ficha do vereador (object_id); 0 = prefeito, Mesa, comissão etc.
            cache['ap' + str(a)] = x['object_id'] if x.get('tipo') == 1 and x.get('object_id') in parl_ids else 0
    salvar_cache(cache)
    R['materias'] = {m: cache[str(m)] for m in mats if str(m) in cache}
    R['autores'] = {a: cache.get('a' + str(a), '') for a in autores}
    R['autores_parl'] = {a: cache.get('ap' + str(a), 0) for a in autores}

    log('fotos')
    pessoas = {m['p'] for m in R['mandatos']} | {p for _, p, _ in R['votos']}
    fotos = paralelo(lambda p: miniatura(next((x['foto'] for x in R['parlamentares'] if x['id'] == p), None)), pessoas, 4)
    R['fotos'] = {p: f for p, f in zip(pessoas, fotos) if f}
    return R


# ---------------------------------------------------------------- emendas parlamentares
def parse_brl(s):
    """Lê valores como 'R$ 80,000,00', 'R$ 1.350,00', '1350' ou '89279.87'."""
    s = re.sub(r'[^\d,.\-]', '', str(s or ''))
    if not s or not re.search(r'\d', s):
        return 0.0
    m = re.search(r'[.,](\d{2})$', s)
    if m:
        return float(re.sub(r'[.,\-]', '', s[:-3]) + '.' + m.group(1))
    if re.fullmatch(r'\d+(\.\d)?', s):
        return float(s)
    return float(re.sub(r'[.,\-]', '', s))


def parse_data(s):
    s = (s or '').strip()[:10]
    m = re.fullmatch(r'(\d{2})/(\d{2})/(\d{4})', s)
    if m:
        return f'{m.group(3)}-{m.group(2)}-{m.group(1)}'
    return s if re.fullmatch(r'\d{4}-\d{2}-\d{2}', s) else None


def chave_nome(s):
    s = unicodedata.normalize('NFD', s or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'\s+', ' ', s.replace(' da ', ' ')).strip()


# ---- órgão e área de cada emenda (coluna NOME_GESTORA da planilha)
# Órgãos conhecidos: (trecho do nome sem acento e em minúsculas, nome para exibir, área)
ORGAOS_CONHECIDOS = [
    (r'fundo municipal de saude', 'Fundo Municipal de Saúde', 'Saúde'),
    (r'gatti', 'Rede Municipal Dr. Mário Gatti', 'Saúde'),
    (r'cultura', 'Cultura e Turismo', 'Cultura e Turismo'),
    (r'\bfada\b|desporto amador', 'FADA - Fundo de Assistência ao Desporto Amador', 'Esporte e Lazer'),
    (r'esportes', 'Esportes e Lazer', 'Esporte e Lazer'),
    (r'servicos publicos', 'Serviços Públicos', 'Cidade e Serviços Urbanos'),
    (r'planejamento', 'Planejamento e Desenvolvimento Urbano', 'Cidade e Serviços Urbanos'),
    (r'economico|tecnologia', 'Desenvolvimento Econômico, Tecnologia e Inovação', 'Desenvolvimento Econômico e Inovação'),
    (r'seg\.? ?publica|seguranca', 'Cooperação nos Assuntos de Segurança Pública', 'Segurança Pública'),
    (r'clima|meio ambiente', 'Clima, Meio Ambiente e Sustentabilidade', 'Meio Ambiente e Clima'),
    (r'servidor|\bfdc\b', 'Fundo de Desenvolvimento e Capacitação do Servidor (FDC)', 'Servidores e Administração'),
    (r'^(secretaria municipal de )?gabinete do prefeito$', 'Gabinete do Prefeito', 'Servidores e Administração'),
    (r'jose pedro de oliveira|\bfjpo\b', 'Fundação José Pedro de Oliveira (Mata de Santa Genebra)', 'Meio Ambiente e Clima'),
    (r'^secretaria municipal de urbanismo$', 'Secretaria de Urbanismo', 'Cidade e Serviços Urbanos'),
    (r'^secretaria municipal de justica$', 'Secretaria de Justiça', 'Servidores e Administração'),
]
# Para órgãos novos: palavras que indicam a área. O que não casar cai em "Outros" e aparece no log.
AREAS_PALAVRAS = [
    ('Saúde', ['saude', 'hospital', 'samu', 'medic']),
    ('Educação', ['educacao', 'escola', 'fumec', 'ensino', 'creche']),
    ('Assistência Social e Direitos', ['assistencia social', 'direitos', 'crianca', 'adolescente', 'idoso', 'mulher', 'deficiencia', 'juventude', 'cidadania', 'igualdade']),
    ('Habitação', ['habitacao', 'cohab', 'moradia']),
    ('Mobilidade e Transporte', ['transporte', 'mobilidade', 'emdec', 'transito']),
    ('Cidade e Serviços Urbanos', ['obras', 'infraestrutura', 'urbano', 'saneamento', 'sanasa', 'zeladoria']),
    ('Cultura e Turismo', ['cultura', 'turismo']),
    ('Esporte e Lazer', ['esporte', 'lazer', 'desporto']),
    ('Segurança Pública', ['seguranca', 'guarda']),
    ('Meio Ambiente e Clima', ['ambiente', 'clima', 'sustentabilidade']),
    ('Desenvolvimento Econômico e Inovação', ['economic', 'tecnologia', 'inovacao', 'renda']),
    ('Servidores e Administração', ['servidor', 'administracao', 'gestao', 'governo', 'financas', 'fazenda', 'recursos humanos', 'procuradoria']),
]


def nome_bonito(s):
    s = re.sub(r'\s+', ' ', s or '').strip()
    if s and s == s.upper():
        s = ' '.join(p if p in ('de', 'da', 'do', 'das', 'dos', 'e') else p.capitalize() for p in s.lower().split(' '))
    return s


def orgao_e_area(nome):
    n = chave_nome(nome)
    for padrao, exibe, area in ORGAOS_CONHECIDOS:
        if re.search(padrao, n):
            return exibe, area
    for area, palavras in AREAS_PALAVRAS:
        if any(p in n for p in palavras):
            return nome_bonito(nome), area
    return nome_bonito(nome) or 'Órgão não informado', 'Outros'


def limpa_obs(s):
    """Texto livre do empenho: junta quebras de linha, tira marcadores internos e omite CPFs."""
    s = re.sub(r'#\{[^}]*\}', ' ', s or '')
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'\b\d{3}\.\d{3}\.\d{3}-\d{2}\b', '(CPF omitido)', s)
    s = re.sub(r'\b\d{11}\b', '(CPF omitido)', s)
    return s[:600].rstrip() + ('…' if len(s) > 600 else '')


def emendas_de_csv(texto, hoje=None):
    """Transforma o CSV da planilha em {total, o:[observações], a:{órgão: área}, v:[{n, pago, forn, r:[[data, fornecedor, pago, secretaria, índice da observação]]}]}.
    Só entram pagamentos já feitos (VL_PAGO > 0). O CPF que vem colado ao nome de pessoa física é removido."""
    leitor = csv.DictReader(io.StringIO(texto))
    if not leitor.fieldnames or 'PARLAMENTAR' not in leitor.fieldnames or 'VL_PAGO' not in leitor.fieldnames:
        raise ValueError('A planilha não tem as colunas esperadas (PARLAMENTAR, VL_PAGO).')
    por = {}
    obs_tab = {'': 0}  # textos repetidos ficam guardados uma vez só
    areas = {}  # órgão -> área
    for lin in leitor:
        nome = re.sub(r'\s+', ' ', (lin.get('PARLAMENTAR') or '')).strip()
        if not nome:
            continue
        # VL_PAGO3 é a coluna já padronizada da planilha; VL_PAGO fica de reserva
        pago = parse_brl(lin.get('VL_PAGO3')) or parse_brl(lin.get('VL_PAGO'))
        d = parse_data(lin.get('DT_CADASTRO'))
        if pago <= 0 or not d:
            continue
        forn = re.sub(r'\s+', ' ', (lin.get('NOME_FORNECEDOR') or '')).strip()
        forn = re.sub(r'\s\d{11}$', '', forn) or 'Fornecedor não informado'
        sec, area = orgao_e_area(lin.get('NOME_GESTORA'))
        areas[sec] = area
        obs = limpa_obs(lin.get('OBS_EMPENHO')) or limpa_obs(lin.get('RESUMO_EMPENHO'))
        por.setdefault(nome, []).append([d, forn, round(pago, 2), sec, obs_tab.setdefault(obs, len(obs_tab))])
    v = []
    for nome, rs in sorted(por.items()):
        rs.sort(key=lambda r: (r[0], r[2]), reverse=True)
        v.append(dict(n=nome, pago=round(sum(r[2] for r in rs), 2), forn=len({r[1] for r in rs}), r=rs))
    return dict(atualizado=hoje or HOJE, total=round(sum(x['pago'] for x in v), 2), o=list(obs_tab), a=areas, v=v)


def emendas_anteriores():
    """Se a planilha falhar, reaproveita as emendas do index.html publicado, para a aba não sumir do site."""
    try:
        html = open('index.html', encoding='utf-8').read()
        m = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
        return json.loads(m.group(1).replace('<\\/', '</')).get('EME') if m else None
    except Exception:
        return None


def coletar_emendas():
    log('Baixando a planilha de emendas...')
    try:
        r = sessao.get(EMENDAS_URL, timeout=120)
        r.raise_for_status()
        texto = r.content.decode('utf-8-sig')
        if texto.lstrip().startswith('<'):
            raise ValueError('O Google devolveu uma página em vez do CSV (a planilha está com acesso restrito?).')
        eme = emendas_de_csv(texto)
        if sum(len(x['r']) for x in eme['v']) < 500:
            raise ValueError('Poucas linhas na planilha de emendas; ignorando.')
        log(f"Emendas: {len(eme['v'])} parlamentares, {sum(len(x['r']) for x in eme['v'])} pagamentos, R$ {eme['total']:,.2f}")
        por_area, por_orgao = collections.Counter(), collections.Counter()
        for x in eme['v']:
            for rr in x['r']:
                por_area[eme['a'].get(rr[3], 'Outros')] += rr[2]
                por_orgao[rr[3]] += rr[2]
        for ar, val in por_area.most_common():
            log(f'  Área {ar}: R$ {val:,.2f}')
        outros = [(o, v) for o, v in por_orgao.most_common() if eme['a'].get(o, 'Outros') == 'Outros']
        if outros:
            log('Órgãos que ficaram em "Outros" (me diga em qual área encaixar): ' + '; '.join(f'{o} (R$ {v:,.0f})' for o, v in outros))
        return eme
    except Exception as e:
        log(f'Aviso: não foi possível atualizar as emendas ({e}).')
        ant = emendas_anteriores()
        if ant:
            log('Mantendo as emendas da última publicação.')
        return ant


# ---------------------------------------------------------------- áreas dos projetos
TIPOS_PROJETO = ('PLO', 'PLC', 'PDL', 'PRE', 'PELOM')
# Áreas dos projetos (ordem importa: a primeira regra que casar define a área). Texto sem acento e minúsculo.
AREAS_PROJ = [
    ('Homenagens e títulos', r'^(concede|outorga|confere)\b|manifestacao de pesar|votos? de pesar|\bpesar pelo|aplauso|congratula|louvor|titulo de cidadao|diploma|medalha|comenda|honra ao merito|voto de (louvor|congratula)'),
    ('Nomes de ruas e locais', r'^(denomina|da denominacao|altera a denominacao)|denomina (rua|avenida|via|praca|travessa|viela|ruela|alameda|estrada|rodovia|sistema de lazer|area|escola|centro|unidade|ponte|viaduto|complexo|parque|bosque|largo)'),
    ('Datas comemorativas', r'calendario (oficial|de datas|de eventos|municipal)|institui,? (no municipio de campinas,? )?(o|a) (dia|semana|mes|jornada|campanha anual)|dia municipal|semana municipal|mes municipal|data comemorativa|inclui no calendario'),
    ('Utilidade pública', r'utilidade publica'),
    ('Funcionamento da Câmara', r'regimento interno|codigo de etica parlamentar|medida disciplinar|camara municipal de campinas|comissao parlamentar|frente parlamentar|tribuna livre|sessao solene|mesa da camara'),
    ('Urbanismo e imóveis públicos', r'terreno baldio|terrenos baldios|area publica|glebas?\b|perimetro urbano|direito real de uso|destinacao de area|condominio|bem publico|bens publicos|imovel|imoveis|desafeta|desincorpora|aliena|doacao de area|concessao de uso|permissao de uso|zoneamento|uso e ocupacao|uso do solo|loteamento|urbanist|plano diretor|impacto de vizinhanca|edifica|construc|\bobras?\b|espacos publicos|hostil'),
    ('Saúde', r'paliativ|diagnostico|equoterapia|valorizacao da vida|suicid|toxicolog|terapia|cegueira|obesidade|urgencia e emergencia|samu|pronto atendimento|sono\b|telessaude|vascular|alzheimer|pressao arterial|respirat|gagueira|aborto|sorriso|bucal|vicio|dependencia quimica|\bsaude\b|hospital|medic|doenca|cancer|vacin|\bsus\b|autis|\btea\b|enferm|covid|dengue|saude mental|gestante|gravidez|parto|obstetr|farmac|diabet|fibromialg|tratamento|paciente|sangue|doacao de orgaos|cirurg|odontol|ubs\b|pronto.?socorro'),
    ('Educação', r'cursinho|formacao profissional|educa|escola|ensino|aluno|estudant|creche|professor|alfabetiza|universit|bolsa de estudo'),
    ('Segurança', r'vigilant|agentes de seguranca|armament|seguranca publica|guarda municipal|guarda civil|violencia|policia|crime|criminal|monitoramento por camera|cameras de|videomonitoramento|defesa civil|drogas|\barmas?\b'),
    ('Meio ambiente e animais', r'\brios?\b|ribeir|plantio|especies|leucena|veterinar|floresta|coleta seletiva|coleta domiciliar|ambient|animal|animais|\bcaes\b|\bgatos\b|\bcao\b|pitbull|focinheira|coleira|arvore|arboriza|residuo|lixo|recicla|clima|\bagua\b|poluic|nascente|mata\b|fauna|flora|sustentab|enchente|drenagem|queimada|pet\b'),
    ('Mobilidade e trânsito', r'bilhete unico|ciclist|bueiro|asfalto|pavimenta|vias publicas|motofret|entregador|transito|transporte|onibus|mobilidade|veicul|estacionamento|ciclo|calcada|motorista|taxi|aplicativo de transporte|semaforo|pedestre|emdec|rodoviaria|aeroporto|\bmotos?\b|motocicl'),
    ('Habitação', r'habitac|moradia|cohab|auxilio moradia|regularizacao fundiaria|ocupacao irregular|loteamento irregular|nucleo residencial'),
    ('Direitos e assistência social', r'racis|pedofilia|adultiza|mulher|feminic|crianca|adolescente|idos|negr[oa]s?\b|lgbt|assistencia social|vulnerab|fome|alimentar|alimentacao|pessoa com deficiencia|deficien|acessibilidade|inclusao|cidadania|direitos humanos|situacao de rua|igualdade|juventude|jovens|familia|refugiad|migrant|cuidador'),
    ('Cultura, esporte e lazer', r'skate|orquestra|cultur|esport|lazer|turismo|festival|musica|artista|\barte\b|teatro|cinema|biblioteca|patrimonio historico|tombamento|capoeira|futebol|atleta|carnaval|feira'),
    ('Economia, tributos e orçamento', r'planta generica|despesas|desburocratiza|publicidade|naming|tribut|imposto|iptu|\biss\b|\bitbi\b|taxa|orcament|credito adicional|credito especial|diretrizes orcamentarias|plano plurianual|fiscal|isencao|anistia|divida|refis|parcelamento|reparcelamento|empresa|comercio|comercial|estabelecimento|consumidor|supermercado|empreend|emprego|trabalho|economi|inovacao|tecnologia|startup|licitac|contrato|combustive'),
    ('Administração e servidores', r'tribunal de contas|lei organica|secretarias municipais|fundacao|poder executivo a|servidor|cargo|carreira|vencimento|gratificac|estrutura administrativa|secretaria municipal|administracao publica|transparencia|ouvidoria|conselho municipal|fundo municipal|previdenc|camprev|quadro de pessoal|subsidio|remunera|autarquia|reajuste'),
]
AREAS_PROJ_RE = [(a, re.compile(p)) for a, p in AREAS_PROJ]


def sem_acento(s):
    return unicodedata.normalize('NFD', s or '').encode('ascii', 'ignore').decode().lower().strip()


def area_projeto(ementa):
    """Área principal do projeto pela ementa: a primeira regra que casar. O que não casar fica em "Outros"."""
    e = sem_acento(ementa)
    return next((a for a, r in AREAS_PROJ_RE if r.search(e)), 'Outros')


# requerimentos, emendas, substitutivos etc. que citam um projeto herdam a área dele
REF_PROJETO = re.compile(r'projeto de (lei complementar|lei ordinaria|lei|decreto legislativo|resolucao)\b[^0-9]{0,25}?(\d+)\s*(?:/|de)\s*(\d{4})')
SIGLA_REF = {'lei complementar': 'PLC', 'lei ordinaria': 'PLO', 'lei': 'PLO', 'decreto legislativo': 'PDL', 'resolucao': 'PRE'}


def areas_das_materias(materias, tipos):
    """Área de cada matéria (projetos, requerimentos, moções, emendas, atas). Devolve {id: área}."""
    sig = {mid: (tipos.get(m['tipo']) or tipos.get(str(m['tipo'])) or {}).get('sigla') for mid, m in materias.items()}
    base = {mid: area_projeto(m['ementa']) for mid, m in materias.items()}
    indice = {}
    for mid, m in materias.items():
        try:
            if sig[mid] in TIPOS_PROJETO:
                indice[(sig[mid], int(m['n']), int(m['ano']))] = mid
        except (TypeError, ValueError):
            pass
    out = {}
    for mid, m in materias.items():
        s, a = sig[mid] or '', base[mid]
        if s.startswith('ATA'):
            a = 'Funcionamento da Câmara'
        elif s not in TIPOS_PROJETO and s != 'MOC':
            r = REF_PROJETO.search(sem_acento(m['ementa']))
            if r:
                alvo = indice.get((SIGLA_REF[r.group(1)], int(r.group(2)), int(r.group(3))))
                a = base[alvo] if alvo is not None else 'Outros'
        out[mid] = a
    return out


def normaliza(s):
    s = (s or '').strip()
    if s == 'Sim': return 'S'
    if s in ('Não', 'Nao'): return 'N'
    if s == 'Abstenção': return 'A'
    if s in ('Presidente não vota', 'Na Presid.'): return 'P'
    return 'F'  # Ausente, Ausente na Votação, -1, Não Votou, vazio e outros


def miniatura(url):
    if not url:
        return None
    try:
        from PIL import Image
        r = sessao.get(url, timeout=60)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert('RGB')
        s = min(im.size)
        x = (im.width - s) // 2
        y = int(max(0, (im.height - s) * 0.2))
        im = im.crop((x, y, x + s, y + s)).resize((160, 160), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=80)
        return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log(f'foto falhou: {url} ({e})')
        return None


def carregar_cache():
    try:
        return json.load(open('cache_materias.json', encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def salvar_cache(c):
    json.dump(c, open('cache_materias.json', 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))


# ---------------------------------------------------------------- montagem
RESULTADOS = {1: 'Aprovado', 2: 'Rejeitado', 3: 'Não votado', 4: 'Mantido', 7: 'Acatado', 8: 'Procedente', 9: 'Improcedente'}


def categoria(rot):
    r = rot.lower()
    if r.startswith('requerimento'): return 'req'
    if r.startswith('moção'): return 'moc'
    if r.startswith('ata'): return 'ata'
    if r.startswith(('projeto', 'emenda', 'substitutivo', 'recurso', 'proposta')): return 'proj'
    return 'out'



# ---------------------------------------------------------------- orçamento da Prefeitura (RREO do 6º bimestre)
# As planilhas ficam na pasta "orcamento" (uma por ano, baixadas do Siconfi). Os nomes de linhas ficam como no relatório.
PASTA_ORC = 'orcamento'
# IPCA do ano (dezembro a dezembro, IBGE) e população de Campinas (estimativa do IBGE; 2022 = Censo; em 2023 o IBGE
# não publicou estimativa e vale o Censo). Anos novos são buscados nas APIs do IBGE; se falhar, a página avisa.
IPCA_ANO = {2016: 6.29, 2017: 2.95, 2018: 3.75, 2019: 4.31, 2020: 4.52, 2021: 10.06, 2022: 5.79, 2023: 4.62, 2024: 4.83, 2025: 4.26}
POP_CAMPINAS = {2016: 1173370, 2017: 1182429, 2018: 1194094, 2019: 1204073, 2020: 1213792, 2021: 1223237,
                2022: 1139047, 2023: 1139047, 2024: 1185977, 2025: 1187974}

def _ipca_ibge(ano):
    try:
        j = requests.get(f'https://apisidra.ibge.gov.br/values/t/1737/n1/all/v/2265/p/{ano}12', timeout=30).json()
        return float(j[1]['V'].replace(',', '.'))
    except Exception as e:
        log(f'IPCA de {ano} não encontrado no IBGE ({e})'); return None

def _pop_ibge(ano):
    try:
        j = requests.get(f'https://servicodados.ibge.gov.br/api/v3/agregados/6579/periodos/{ano}/variaveis/9324?localidades=N6[3509502]', timeout=30).json()
        return int(j[0]['resultados'][0]['series'][0]['serie'][str(ano)])
    except Exception as e:
        log(f'População de {ano} não encontrada no IBGE ({e})'); return None

def num(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def recuo(s): return len(s) - len(s.lstrip(' '))

def linhas(sh, ini, fim):
    for r in range(ini, fim):
        row = [c.value for c in sh.row(r)]
        if not str(row[0]).strip(): continue
        yield str(row[0]), row

def quadro(sh, titulo_ini):
    """intervalo de linhas de dados do quadro cujo título começa com titulo_ini"""
    ini = None
    for r in range(sh.nrows):
        t = str(sh.cell_value(r, 0)).strip()
        if t.startswith('Quadro:'):
            if ini is not None: return ini, r
            if t[7:].strip().startswith(titulo_ini): ini = r + 1
    return ini, sh.nrows

def anexo01(b):
    sh = b.sheet_by_name('RREO-Anexo 01')
    rec = []; i, f = quadro(sh, 'Receitas Orçament')
    dentro = False
    for nome, row in linhas(sh, i, f):
        n = nome.strip()
        if n.startswith('RECEITAS (EXCETO INTRA'): dentro = True
        elif dentro and (n.startswith('RECEITAS (INTRA') or n.startswith('SUBTOTAL') or n.startswith('OPERAÇÕES DE CRÉDITO / REFINANC')): break
        if dentro: rec.append(dict(n=n, nivel=recuo(nome), ini=num(row[1]), atu=num(row[2]), real=num(row[5])))
    des = []; i, f = quadro(sh, 'Despesas Orçament')
    dentro = False
    for nome, row in linhas(sh, i, f):
        n = nome.strip()
        if n.startswith('DESPESAS (EXCETO INTRA'): dentro = True
        elif dentro and (n.startswith('DESPESAS (INTRA') or n.startswith('SUBTOTAL')): break
        if dentro: des.append(dict(n=n, nivel=recuo(nome), ini=num(row[1]), atu=num(row[2]), emp=num(row[4]), liq=num(row[7]), pago=num(row[9])))
    return rec, des

def anexo02(b):
    sh = b.sheet_by_name('RREO-Anexo 02')
    i, f = quadro(sh, 'Função/Subfunção')
    out = []; dentro = False
    for nome, row in linhas(sh, i, f):
        n = nome.strip()
        if n.startswith('DESPESAS (EXCETO INTRA'): dentro = True; total = dict(ini=num(row[1]), atu=num(row[2]), emp=num(row[4]), liq=num(row[8])); continue
        if dentro and (n.startswith('DESPESAS (INTRA') or n.startswith('TOTAL')): break
        if dentro: out.append(dict(n=n, nivel=recuo(nome), ini=num(row[1]), atu=num(row[2]), emp=num(row[4]), liq=num(row[8])))
    return total, out

def anexo03(b):
    sh = b.sheet_by_name('RREO-Anexo 03')
    col = None
    for r in range(sh.nrows):
        for c in range(sh.ncols):
            if str(sh.cell_value(r, c)).strip().upper().startswith('TOTAL (ÚLTIMOS'): col = c; break
        if col is not None: break
    out = []
    i, f = quadro(sh, ''); 
    for r in range(sh.nrows):
        n = str(sh.cell_value(r, 0)).strip()
        if n.startswith('RECEITAS CORRENTES (I)'): ini = r; break
    for r in range(ini, sh.nrows):
        n = str(sh.cell_value(r, 0)).strip()
        if n.startswith('RECEITA CORRENTE LÍQUIDA (III)'): out.append(dict(n=n, v=num(sh.cell_value(r, col)))); break
        if n: out.append(dict(n=n, v=num(sh.cell_value(r, col))))
    return out

def ler(pasta):
    anos = {}
    for arq in sorted(glob.glob(os.path.join(pasta, '*.xls'))):
        b = xlrd.open_workbook(arq)
        sh = b.sheet_by_name('RREO-Anexo 01')
        ano = per = None
        for r in range(15):
            t = str(sh.cell_value(r, 0))
            m = re.search(r'Exercício:\s*(\d{4})', t); ano = ano or (m and int(m.group(1)))
            if 'Período:' in t: per = t.split(':', 1)[1].strip()
        if not per or not per.startswith('6'):
            print(f'{arq}: período "{per}" não é o 6º bimestre; ignorado'); continue
        rec, des = anexo01(b); tf, fun = anexo02(b)
        anos[ano] = dict(rec=rec, des=des, funTotal=tf, fun=fun, rcl=anexo03(b))
    return anos


# ---------------------------------------------------------------- montagem para a página
def _linha(sh_rows, prefixo):
    for x in sh_rows:
        if x['n'].startswith(prefixo): return x
    return None

def extras_anexo01(b):
    """linhas que ficam depois do subtotal: refinanciamento e as intra-orçamentárias"""
    sh = b.sheet_by_name('RREO-Anexo 01')
    out = {}
    for r in range(sh.nrows):
        n = re.sub(r'\s*/\s*', ' / ', str(sh.cell_value(r, 0)).strip()); row = [c.value for c in sh.row(r)]
        if n.startswith('OPERAÇÕES DE CRÉDITO / REFINANCIAMENTO') and 'ref_rec' not in out: out['ref_rec'] = dict(ini=num(row[1]) or 0, atu=num(row[2]) or 0, real=num(row[5]) or 0)
        if n.startswith('AMORTIZAÇÃO DA DÍVIDA / REFINANCIAMENTO') and 'ref_des' not in out: out['ref_des'] = dict(ini=num(row[1]) or 0, atu=num(row[2]) or 0, liq=num(row[7]) or 0)
        if n.startswith('RECEITAS (INTRA') and 'intra_rec' not in out: out['intra_rec'] = num(row[5]) or 0
        if n.startswith('DESPESAS (INTRA') and 'intra_des' not in out: out['intra_des'] = num(row[7]) or 0
    return out

def montar_ano(arq):
    b = xlrd.open_workbook(arq)
    rec, des = anexo01(b); tf, fun = anexo02(b); rcl = anexo03(b); ex = extras_anexo01(b)
    z = lambda v: v or 0
    def soma(lista, *prefixos, nivel=None):
        return sum(z(x['real']) for x in lista if (nivel is None or x['nivel'] == nivel) and any(x['n'].startswith(p) for p in prefixos))
    # blocos do anexo 1 (receitas): correntes e capital
    ic = next(i for i, x in enumerate(rec) if x['n'] == 'RECEITAS CORRENTES')
    ik = next(i for i, x in enumerate(rec) if x['n'] == 'RECEITAS DE CAPITAL')
    cor, cap = rec[ic + 1:ik], rec[ik + 1:]
    def filhos(bloco, pai):
        i = next((k for k, x in enumerate(bloco) if x['n'].upper().startswith(pai)), None)
        if i is None: return []
        nv = bloco[i]['nivel']; out = []
        for x in bloco[i + 1:]:
            if x['nivel'] <= nv: break
            out.append(x)
        return out
    def pai(bloco, nome):
        x = next((x for x in bloco if x['n'].upper().startswith(nome)), None); return z(x and x['real'])
    total = rec[0]; ref = ex.get('ref_rec', dict(ini=0, atu=0, real=0))
    R = dict(real=z(total['real']) + ref['real'], ini=z(total['ini']) + ref['ini'], atu=z(total['atu']) + ref['atu'])
    # por imposto e repasse (anexo 3, valores cheios) + receitas de capital (anexo 1) − retenção para o FUNDEB
    v3 = {x['n']: z(x['v']) for x in rcl}
    def a3(*nomes):
        return sum(v for n, v in v3.items() if any(n.startswith(p) for p in nomes))
    imp = [
        ('ISS', a3('ISS')), ('IPTU', a3('IPTU')), ('ITBI', a3('ITBI')), ('IRRF', a3('IRRF')),
        ('TRIB', a3('Outras Receitas Tributárias', 'Outros Impostos, Taxas')),
        ('CONTRIB', a3('Receita de Contribuições', 'Contribuições')),
        ('PATRIM', a3('Receita Patrimonial')), ('SERV', a3('Receita de Serviços', 'Receita Agropecuária', 'Receita Industrial')),
        ('OUTCOR', a3('Outras Receitas Correntes')),
        ('ICMS', a3('Cota-Parte do ICMS')), ('IPVA', a3('Cota-Parte do IPVA')), ('LC61', a3('Transferências da LC nº 61')),
        ('FPM', a3('Cota-Parte do FPM')), ('ITR', a3('Cota-Parte do ITR')), ('LC87', a3('Transferências da LC nº 87')),
        ('FUNDEB', a3('Transferências do FUNDEB')), ('OUTTRANSF', a3('Outras Transferências Correntes')),
        ('CRED', pai(cap, 'OPERAÇÕES DE CRÉDITO') + ref['real']), ('TCAP', pai(cap, 'TRANSFERÊNCIAS DE CAPITAL')),
        ('OUTCAP', pai(cap, 'ALIENAÇÃO') + pai(cap, 'AMORTIZAÇÕES') + pai(cap, 'OUTRAS RECEITAS DE CAPITAL')),
        ('DEDFUNDEB', -a3('Dedução de Receita para Formação do FUNDEB')),
    ]
    rc_bruta = a3('RECEITAS CORRENTES (I)')
    # por quem arrecada (anexo 1, valores líquidos)
    tc = filhos(cor, 'TRANSFERÊNCIAS CORRENTES'); tk = filhos(cap, 'TRANSFERÊNCIAS DE CAPITAL')
    def st(nomes): return sum(z(x['real']) for x in tc + tk if any(x['n'].startswith(p) for p in nomes))
    uniao = st(['Transferências da União']); estado = st(['Transferências dos Estados'])
    outras = st(['Transferências de Outras Instituições', 'Transferências de Instituições Privadas', 'Transferências dos Municípios', 'Transferências do Exterior', 'Demais Transferências', 'Transferências de Pessoas', 'Transferências para o Combate'])
    semdet = st(['Transferências Intergovernamentais', 'Transferências de Convênios'])
    transf_total = pai(cor, 'TRANSFERÊNCIAS CORRENTES') + pai(cap, 'TRANSFERÊNCIAS DE CAPITAL')
    cred = pai(cap, 'OPERAÇÕES DE CRÉDITO') + ref['real']
    cidade = R['real'] - transf_total - cred
    esf = [('CIDADE', cidade), ('ESTADO', estado), ('UNIAO', uniao), ('OUTINST', outras), ('SEMDET', semdet), ('CRED', cred)]
    resto = transf_total - (uniao + estado + outras + semdet)
    if abs(resto) > 1: esf.append(('SEMDET', resto))
    # despesas por tipo (anexo 1)
    def d1(nome):
        x = next((x for x in des if x['n'].startswith(nome)), None)
        return [z(x and x['liq']), z(x and x['ini']), z(x and x['atu'])]
    rd = ex.get('ref_des', dict(ini=0, atu=0, liq=0)); am = d1('AMORTIZAÇÃO DA DÍVIDA')
    tipo = [('PESSOAL', *d1('PESSOAL E ENCARGOS')), ('CUSTEIO', *d1('OUTRAS DESPESAS CORRENTES')), ('INVEST', *d1('INVESTIMENTOS')),
            ('JUROS', *d1('JUROS E ENCARGOS')), ('AMORT', am[0] + rd['liq'], am[1] + rd['ini'], am[2] + rd['atu']), ('INVERS', *d1('INVERSÕES FINANCEIRAS')),
            ('RESERVA', *d1('RESERVA DE CONTINGÊNCIA'))]
    # despesas por área (anexo 2): funções e subfunções, nomes como no relatório
    F = []
    for x in fun:
        if x['nivel'] == 2: F.append([x['n'], z(x['liq']), z(x['ini']), z(x['atu']), []])
        elif F and z(x['liq']) > 0: F[-1][4].append([x['n'], z(x['liq'])])
    F = [f for f in F if f[1] or f[2] or f[3]]
    D = dict(liq=z(tf['liq']), emp=z(tf['emp']), ini=z(tf['ini']), atu=z(tf['atu']))
    # conferências
    alertas = []
    if abs(sum(v for _, v in imp) - R['real']) > 1e6: alertas.append(f"imposto: soma {sum(v for _, v in imp):.0f} x arrecadado {R['real']:.0f}")
    if abs(sum(v for _, v in esf) - R['real']) > 1e3: alertas.append('quem arrecada não fecha')
    if abs(sum(t[1] for t in tipo) - D['liq']) > 1e6: alertas.append(f"tipo: soma {sum(t[1] for t in tipo):.0f} x gasto {D['liq']:.0f}")
    if abs(sum(f[1] for f in F) - D['liq']) > 1e6: alertas.append('áreas não fecham')
    r = lambda v: round(v, 2)
    return dict(rec=dict((k, r(v)) for k, v in R.items()), des=dict((k, r(v)) for k, v in D.items()),
                imp=[[k, r(v)] for k, v in imp if abs(v) >= 0.5], esf=[[k, r(v)] for k, v in esf if abs(v) >= 0.5],
                tipo=[[k, r(a), r(b_), r(c)] for k, a, b_, c in tipo if a or b_ or c],
                fun=[[n, r(a), r(b_), r(c), [[s, r(v)] for s, v in sub]] for n, a, b_, c, sub in F],
                intra=dict(rec=r(ex.get('intra_rec', 0)), des=r(ex.get('intra_des', 0))), alertas=alertas)


def ler_orcamento():
    if not os.path.isdir(PASTA_ORC) or not glob.glob(os.path.join(PASTA_ORC, '*.xls')):
        return None
    global xlrd
    try:
        import xlrd
    except ImportError:
        import subprocess; subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'xlrd'], check=False); import xlrd
    anos = {}
    for arq in sorted(glob.glob(os.path.join(PASTA_ORC, '*.xls'))):
        try:
            b = xlrd.open_workbook(arq); sh = b.sheet_by_name('RREO-Anexo 01')
            ano = per = None
            for r in range(15):
                t = str(sh.cell_value(r, 0)); m = re.search(r'Exercício:\s*(\d{4})', t)
                if m: ano = int(m.group(1))
                if 'Período:' in t: per = t.split(':', 1)[1].strip()
            if not ano or not per or not per.startswith('6'):
                log(f'Orçamento: {arq} não é o relatório do 6º bimestre; ficou de fora'); continue
            d = montar_ano(arq)
            for a in d.pop('alertas'): log(f'Orçamento {ano}: conferir ({a})')
            anos[ano] = d
        except Exception as e:
            log(f'Orçamento: não consegui ler {arq} ({e})')
    if not anos:
        return None
    ipca = {a: IPCA_ANO.get(a) or _ipca_ibge(a) for a in anos}
    pop = {a: POP_CAMPINAS.get(a) or _pop_ibge(a) for a in anos}
    log(f'Orçamento: {len(anos)} anos ({min(anos)} a {max(anos)})')
    return dict(anos={str(a): v for a, v in sorted(anos.items())}, ipca={str(a): v for a, v in ipca.items()}, pop={str(a): v for a, v in pop.items()})

def montar(R, saida='index.html'):
    hoje = R.get('hoje') or HOJE
    leg = R.get('legislatura') or {}
    atual = leg.get('atual', True)
    sess = {s['id']: s for s in R['sessoes']}
    parl = {p['id']: p for p in R['parlamentares']}
    sigla_partido = {p['id']: p['sigla'] for p in R['partidos']}
    fil = collections.defaultdict(list)
    for f in R['filiacoes']:
        fil[f['p']].append(f)

    def partido(pid):
        vig = [f for f in fil.get(pid, []) if f['ini'] and f['ini'] <= hoje and (not f['fim'] or f['fim'] >= hoje)]
        f = max(vig or fil.get(pid, [{'ini': ''}]), key=lambda f: f['ini'] or '')
        return sigla_partido.get(f.get('partido'))

    mand = {}
    for m in R['mandatos']:
        if m['p'] not in mand or (m['ini'] or '') < (mand[m['p']]['ini'] or ''):
            mand[m['p']] = m
    votos_por = collections.defaultdict(dict)
    for v, p, c in R['votos']:
        votos_por[v][p] = c
    nominais = {v for v, d in votos_por.items() if any(c in 'SNA' for c in d.values())}
    pres = collections.defaultdict(set)
    for s, p in R['presenca']:
        pres[s].add(p)
    comp = {c['id']: c['comissao'] for c in R['composicoes']}
    cargos = {int(k): v for k, v in R['cargos'].items()}

    pessoas = set(mand) | {p for d in votos_por.values() for p in d}
    V = []
    for pid in pessoas:
        p = parl.get(pid)
        if not p:
            continue
        m = mand.get(pid)
        cnt, alin = collections.Counter(), [0, 0]
        for vid in nominais:
            c = votos_por[vid].get(pid)
            if not c:
                continue
            cnt[c] += 1
            s_ = sum(1 for x in votos_por[vid].values() if x == 'S')
            n_ = sum(1 for x in votos_por[vid].values() if x == 'N')
            if c in 'SN' and s_ != n_:
                alin[1] += 1
                alin[0] += (c == 'S') == (s_ > n_)
        com = [[comp[x['comp']], cargos.get(x['cargo'], ''), x['titular']] for x in R['participacoes']
               if x['p'] == pid and (not x['fim'] or x['fim'] >= hoje) and x['comp'] in comp]
        ativo = p['ativo'] if atual else any(x['p'] == pid and (not x['fim'] or x['fim'] >= hoje) for x in R['mandatos'])
        V.append(dict(id=pid, nome=p['nome'], completo=p['completo'], ativo=ativo, partido=partido(pid),
                      email=p['email'], tel=p['tel'], prof=p['prof'],
                      mand=[m['ini'], m['fim'], bool(m['titular'])] if m else None,
                      votos=dict(cnt), alin=alin, com=com))
    V.sort(key=lambda x: x['nome'])

    VT = []
    for v in R['votacoes']:
        if v['s'] not in sess:
            continue
        VT.append([v['id'], v['m'], v['s'], v['fase'], v['res'], 0, 0, 0, v['mat'], categoria(v['mat']), v['turno']])
    VT.sort(key=lambda r: (sess[r[2]]['d'], r[0]), reverse=True)

    tipos = {int(k): v for k, v in R['tipos_materia'].items()}
    autores = {int(k): v for k, v in R['autores'].items()}
    MAT = {}
    for k, m in R['materias'].items():
        MAT[str(k)] = [tipos.get(m['tipo'], {}).get('sigla'), m['n'], m['ano'], m['ementa'],
                       [autores.get(a, '') for a in (m['autores'] or [])], m['texto']]
    autp = {int(k): v for k, v in (R.get('autores_parl') or {}).items()}
    nomes_areas = [a for a, _ in AREAS_PROJ] + ['Outros']
    area_m = {str(k): nomes_areas.index(a) for k, a in areas_das_materias(R['materias'], tipos).items()}
    for k, linha in MAT.items():
        linha.append(area_m.get(k, len(nomes_areas) - 1))  # posição 6: índice da área
    PROJ = []
    for mid, data in R.get('projetos') or []:
        m = R['materias'].get(mid) or R['materias'].get(str(mid))
        if not m:
            continue
        PROJ.append([mid, data, area_m[str(mid)], [autp.get(a, 0) for a in (m['autores'] or [])]])
    if PROJ:
        cont = collections.Counter(nomes_areas[p[2]] for p in PROJ)
        log(f'Projetos por área ({len(PROJ)}): ' + '; '.join(f'{a} {n}' for a, n in cont.most_common()))
        outros = [R['materias'].get(p[0]) or R['materias'].get(str(p[0])) for p in PROJ if nomes_areas[p[2]] == 'Outros']
        if outros:
            log('Projetos que ficaram em "Outros" (me diga em qual área encaixar): ' + ' | '.join((m['ementa'] or '')[:90] for m in outros[:40]))
    usadas = {c[0] for v in V for c in v['com']} | {r['c'] for r in R['reunioes']} | {r['c'] for r in R['relatorias']}
    COM = {str(c['id']): [c['nome'], c['sigla'], c['tipo']] for c in R['comissoes'] if c['id'] in usadas}

    dados = dict(
        gerado=R['gerado_em'], hoje=hoje, LEG=dict(n=leg.get('numero'), ini=leg.get('inicio'), fim=leg.get('fim'), atual=atual), res={str(k): v for k, v in RESULTADOS.items()},
        V=V, S={s['id']: [s['n'], s['d'], s['t'], s['ata'], s['pauta']] for s in R['sessoes']},
        VT=VT, NOM={vid: ','.join(f'{p}{c}' for p, c in votos_por[vid].items()) for vid in nominais},
        MAT=MAT, COM=COM,
        PRES={s: ','.join(map(str, sorted(ps))) for s, ps in pres.items()},
        TIPOS={t['sigla']: t['desc'] for t in tipos.values()},
        FOTOS={str(k): v for k, v in R['fotos'].items()},
        REU=[[r['id'], r['c'], r['n'], r['nome'], r['tema'], r['d'], r['hi'], r['hf'], r['local'], r['obs'],
              r['video'], r['audio'], r['pauta'], r['ata'], r['anexo']] for r in R['reunioes']],
        REL=[[r['m'], r['p'], r['c'], r['ini'], r['fim'], None, r['tfim']] for r in R['relatorias'] if r['ini']],
        TFIM={str(k): v for k, v in R['tipos_fim'].items()},
    )
    dados['AREAS_P'] = nomes_areas
    if PROJ:
        dados['PROJ'] = PROJ
    if atual:
        orc = ler_orcamento()
        if orc:
            dados['ORC'] = orc
    if atual and R.get('emendas'):
        dados['EME'] = R['emendas']
        nomes = {chave_nome(v['nome']) for v in V}
        sem = sorted(x['n'] for x in R['emendas']['v'] if chave_nome(x['n']) not in nomes)
        if sem:
            log('Emendas de parlamentares sem página nesta legislatura (ou com nome diferente do SAPL): ' + ', '.join(sem))
    js = json.dumps(dados, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    html = open('modelo.html', encoding='utf-8').read().replace('__DATA__', js)
    open(saida, 'w', encoding='utf-8').write(html)
    log(f'{saida} gerado: {len(html)//1024} KB, {len(VT)} votações, {len(nominais)} nominais, '
        f'{len(R["reunioes"])} reuniões, {len(dados["FOTOS"])} fotos')
    return dados


def ler_bruto(arq):
    abre = gzip.open if arq.endswith('.gz') else open
    with abre(arq, 'rt', encoding='utf-8') as f:
        return json.load(f)


def refazer_legislaturas():
    """Remonta cada leg-N.html com o modelo.html atual, usando a coleta guardada em leg-N.json.gz."""
    for arq in sorted(glob.glob('leg-*.json.gz')):
        R = ler_bruto(arq)
        saida = arq[:-len('.json.gz')] + '.html'
        d = montar(R, saida)
        log(f'{saida} refeita com o modelo atual ({len(d["VT"])} votações)')


if __name__ == '__main__':
    if '--refazer-legs' in sys.argv:
        refazer_legislaturas()
        sys.exit(0)
    leg_num = int(sys.argv[sys.argv.index('--leg') + 1]) if '--leg' in sys.argv else None
    if '--bruto' in sys.argv:
        R = ler_bruto(sys.argv[sys.argv.index('--bruto') + 1])
    else:
        R = coletar(leg_num)
        if not leg_num:
            R['emendas'] = coletar_emendas()
        json.dump(R, open('coleta.json', 'w', encoding='utf-8'), ensure_ascii=False)
        if leg_num:
            # guarda a coleta da legislatura anterior (compactada) para refazer a página quando o modelo mudar
            # (o repositório é público: contatos só de quem teve mandato nessa legislatura, como já aparece no site)
            com_mandato = {m['p'] for m in R['mandatos']}
            for x in R['parlamentares']:
                if x['id'] not in com_mandato:
                    x['email'] = x['tel'] = None
            with gzip.open(f"leg-{R['legislatura']['numero']}.json.gz", 'wt', encoding='utf-8') as f:
                json.dump(R, f, ensure_ascii=False, separators=(',', ':'))
    saida = f"leg-{R['legislatura']['numero']}.html" if leg_num else 'index.html'
    d = montar(R, saida)
    if len(d['VT']) < 100 or len(d['V']) < 20:
        sys.exit('Coleta incompleta: index.html não deve ser publicado.')
