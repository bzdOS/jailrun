PORTNAME=	jailrun
DISTVERSION=	1.0.0.a2
CATEGORIES=	sysutils python

MAINTAINER=	ap.bodrov@gmail.com
COMMENT=	Native-first OCI runtime for FreeBSD jails
WWW=		https://github.com/bzdOS/jailrun

LICENSE=	MIT
LICENSE_FILE=	${WRKSRC}/LICENSE

BUILD_DEPENDS=	${PYTHON_PKGNAMEPREFIX}setuptools>=68:devel/py-setuptools@${PY_FLAVOR}
RUN_DEPENDS=	skopeo:sysutils/skopeo \
		ca_root_nss:security/ca_root_nss

USES=		python:3.10+ shebangfix
USE_PYTHON=	distutils autoplist
SHEBANG_FILES=	bin/jailrun

USE_GITHUB=	yes
GH_ACCOUNT=	bzdOS
GH_PROJECT=	jailrun
GH_TAGNAME=	v1.0.0-alpha.2

NO_ARCH=	yes

SUB_FILES=	pkg-message pkg-install

post-install:
	@${MKDIR} ${STAGEDIR}${PREFIX}/share/man/man1/
	${INSTALL_MAN} ${WRKSRC}/man/man1/jailrun.1 \
		${STAGEDIR}${PREFIX}/share/man/man1/

.include <bsd.port.mk>
