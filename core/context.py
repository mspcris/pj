"""Context processor: injeta o UsuarioPermitido logado em todo template."""
from .models import UsuarioPermitido


def usuario_pj(request):
    up = None
    if request.user.is_authenticated:
        up = UsuarioPermitido.objects.filter(
            email=request.user.email.lower(), ativo=True
        ).select_related('prestador').first()
    return {'usuario_pj': up}
