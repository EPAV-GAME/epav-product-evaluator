from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Decision(StrictModel):
    no_id: Identifier
    opcao_id: Identifier

class Quantity(StrictModel):
    unidades: int = Field(ge=1, le=1000, strict=True)
    peso_total_kg: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)

class EvaluationRequest(StrictModel):
    roteiro: Literal['legado', 'ia-v2'] = 'legado'
    cliente_id: Identifier
    no_atual: Identifier
    produto_id: Identifier
    historico: list[Decision] = Field(default_factory=list, max_length=40)
    quantidade: Quantity | None = None
    observacao_jogador: str = Field(default='', max_length=600)
    categoria: Literal['entrada', 'principal', 'acompanhamento', 'bebida', 'sobremesa'] | None = None
    escolhas_anteriores: list['MenuSelection'] = Field(default_factory=list, max_length=4)

class MenuSelection(StrictModel):
    categoria: Literal['entrada', 'principal', 'acompanhamento', 'bebida', 'sobremesa']
    produto_id: Identifier
    quantidade: Quantity

class RecommendationRequest(StrictModel):
    roteiro: Literal['legado', 'ia-v2'] = 'legado'
    cliente_id: Identifier
    no_atual: Identifier
    historico: list[Decision] = Field(default_factory=list, max_length=40)
    categoria: Literal['entrada', 'principal', 'acompanhamento', 'bebida', 'sobremesa'] | None = None

class ProductCard(StrictModel):
    id: str
    codigo: str
    nome: str
    tiposProduto: list[str]
    ocasioes: list[str]
    marca: str | None
    formato: str | None
    unidade_medida: str | None
    peso_embalagem_kg: float | None
    imagem_url: str

class CatalogCodesRequest(StrictModel):
    codigos: list[Annotated[str, Field(pattern=r'^\d{1,12}$')]] = Field(min_length=1, max_length=25)

class RecommendationResponse(StrictModel):
    cliente_id: str
    no_atual: str
    produtos: list[ProductCard] = Field(min_length=0, max_length=10)
    ficha_escuta: list[str]
    orientacao: str
    categoria: Literal['entrada', 'principal', 'acompanhamento', 'bebida', 'sobremesa'] | None = None
    total_disponiveis: int = 0
    quantidade_solicitada: int = 3

class Criterion(StrictModel):
    nota: int = Field(ge=0, le=100, strict=True)
    justificativa: str = Field(min_length=1, max_length=400)
    evidencias: list[str] = Field(max_length=5)

class AIJudgement(StrictModel):
    necessidade: Criterion
    ocasiao: Criterion
    praticidade: Criterion
    restricoes: Criterion
    quantidade: Criterion
    resumo: str = Field(min_length=1, max_length=600)
    sugestao: str = Field(min_length=1, max_length=500)
    informacoes_faltantes: list[str] = Field(max_length=8)
    # Legacy advisory flag; the rubric derives proven conflicts from server facts
    # and never uses this flag to calculate a score.
    contradicao_explicita: bool = False

class EvaluationResponse(StrictModel):
    score: int = Field(ge=0, le=1000)
    percentual_adequacao: float = Field(ge=0, le=100)
    classificacao: Literal['excelente', 'boa', 'parcial', 'inadequada']
    criterios: dict[str, Criterion]
    resumo: str
    sugestao: str
    informacoes_faltantes: list[str]
    versao_rubrica: str
    modelo: str
    contexto_hash: str
    produto_id: str
    cliente_id: str
