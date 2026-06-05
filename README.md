# German Supermarket Deals

Flask 기반의 독일 슈퍼마켓/드럭스토어 할인 대시보드입니다. 기본 화면(`/`)은 ALDI Nord, EDEKA, REWE, Lidl, Netto Marken-Discount, PENNY, nahkauf, Kaufland의 주간 할인 항목을 가져와 필터링과 추천 후보로 보여줍니다. ALDI Nord, EDEKA, REWE, Kaufland는 공식 할인 페이지를 우선 사용하고, 공식 요청이 일시적으로 실패하면 Marktguru 데이터로 대체합니다. Lidl, Netto Marken-Discount, PENNY, nahkauf는 Marktguru 공개 데이터를 사용합니다.

드럭스토어 화면(`/drogerie`)은 dm, ROSSMANN, budni를 슈퍼마켓과 별도로 관리합니다. dm은 공식 dm.de Ausverkauf/Angebot 검색 결과를 사용합니다. ROSSMANN은 공식 Blätterkatalog 색인으로 전단지 항목을 대조하고, 가격/이미지처럼 구조화가 필요한 상세값은 Marktguru 데이터를 함께 사용합니다. budni는 Marktguru 공개 데이터를 사용합니다. 드럭스토어도 `Diese Woche / 이번 주`와 `Nächste Woche / 다음 주` 탭으로 분리해 볼 수 있습니다. 두 화면 모두 독일어 원문을 우선 보여주고, 옆에 한국어 보조 번역을 함께 표시합니다.

## 실행

```bash
source .venv/bin/activate
flask --app run run --debug
```

기본 접속 주소는 `http://127.0.0.1:5000` 입니다.

휴대폰에서 같은 화면을 보려면 컴퓨터와 휴대폰을 같은 Wi-Fi에 연결한 뒤, 개발 서버를 네트워크에 열어 실행합니다.

```bash
source .venv/bin/activate
flask --app run run --debug --host 0.0.0.0
```

그다음 휴대폰 브라우저에서 `http://컴퓨터-IP:5000`으로 접속합니다. 예를 들어 컴퓨터 IP가 `192.168.0.12`라면 `http://192.168.0.12:5000`입니다.

## 구조

- `app/routes.py`: Flask 컨트롤러
- `app/templates/`: 화면(View)
- `app/models.py`: 할인 항목 모델
- `app/services/`: 필터링, 캐시, 카테고리 분류
- `app/providers/`: 외부 데이터 소스 어댑터

나중에 알림 기능을 붙일 때는 `providers`는 그대로 두고, 관심상품 저장소와 알림 서비스만 추가하면 됩니다.

## 설정

환경변수로 기본값을 바꿀 수 있습니다.

- `DEFAULT_ZIP_CODE`: 기본 우편번호, 기본값 `14195`
- `CACHE_TTL_SECONDS`: 캐시 유지 시간, 기본값 6시간
- `MAX_OFFERS_PER_RETAILER`: 마트별 최대 수집 항목, 기본값 `60`
- `REQUEST_VERIFY_TLS`: `true`, `false`, `auto`; 기본값 `auto`

## 테스트

```bash
.venv/bin/pytest -q
```
