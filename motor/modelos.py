"""Estruturas de dados compartilhadas pelo motor."""

from dataclasses import dataclass, field


@dataclass
class Entrega:
    """Uma parada de entrega."""
    id: str                    # identificador estável (ex: CÓDIGO do pedido)
    lat: float
    lng: float
    nome: str = ""             # nome do cliente (exibição) — pode repetir
    obs: str = ""              # observação livre (ex: "só após 14h")
    # Janela de horário opcional, em minutos desde o início da roteirização.
    # None = sem restrição (entrega pode ser feita a qualquer momento).
    janela_inicio: int | None = None
    janela_fim: int | None = None


@dataclass
class Entregador:
    """Um entregador disponível no dia. A rota dele termina na casa dele."""
    id: str
    nome: str
    lat: float                 # endereço de casa
    lng: float


@dataclass
class CD:
    """Centro de distribuição — origem de todas as rotas."""
    lat: float
    lng: float
    nome: str = "CD"


@dataclass
class Parada:
    """Entrega dentro de uma rota, já com a ordem e o tempo estimado de chegada."""
    entrega: Entrega
    ordem: int                 # posição na rota (1 = primeira)
    chegada_estimada_s: int    # segundos desde a saída do CD


@dataclass
class Rota:
    """Resultado: uma sequência de entregas atribuída a um entregador (ou Lalamove)."""
    entregador: Entregador | None      # None = rota candidata a app (Lalamove)
    paradas: list[Parada] = field(default_factory=list)
    distancia_m: int = 0
    duracao_s: int = 0
    candidata_lalamove: bool = False

    @property
    def n_paradas(self) -> int:
        return len(self.paradas)
