"""Context processor: injeta o UsuarioPermitido logado (respeitando o modo
"ver como" do admin) em todo template."""
from .views import _usuario


def usuario_pj(request):
    return {'usuario_pj': _usuario(request),
            'ver_como_ativo': request.session.get('ver_como')}
