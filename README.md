# Refoss Cloud for Home Assistant

Refoss EM06을 Home Assistant에서 사용하기 위한 비공식 커스텀 통합입니다.

이 통합은 **Refoss 클라우드 HTTP API**와 **EM06 로컬 `/public` API**를 함께 사용해 채널별 전력 정보를 가져옵니다.

제공하는 센서는 다음과 같습니다.

- 검침일 기준 월 사용량
- 오늘 사용량
- 현재 전력
- 전압
- 역률
- 전류

태양광 패널이 연결된 채널처럼 순사용량이 음수가 될 수 있는 환경도 그대로 처리합니다.

## 주요 기능

- Home Assistant GUI 설정 지원
- Refoss 계정 로그인 후 EM06 자동 검색
- Refoss Cloud 로그인으로 로컬 API 서명용 `key` 확보
- EM06 로컬 `/public` API를 통한 현재값 조회
- Refoss Cloud HTTP history API를 통한 과거 1시간 단위 사용량 복구
- 채널별 센서 생성
  - A1
  - B1
  - C1
  - A2
  - B2
  - C2
- 검침일 선택
  - 1일 ~ 27일
  - 말일
- 업데이트 주기 설정
  - 기본 15초
  - 최소 10초
- 채널별 센서 제공
  - Billing month energy
  - This Day Energy
  - Power
  - Voltage
  - PF
  - Current
- 태양광 채널의 음수 순사용량 유지
- 정전, 로컬 API 오류, 일부 데이터 누락 시에도 센서 생성 유지
- 로컬 API `sign error` 발생 시 Cloud login/key 갱신 후 재시도
- `ConsumptionH` 채널 조회를 1,2,3 → 4,5,6 순서의 배치로 처리
- 완료된 시간대 사용량을 `.storage` ledger 파일에 저장

## 지원 대상

- Refoss EM06
- Home Assistant 커스텀 통합

현재 구현은 EM06을 기준으로 작성되었습니다.

## 데이터 조회 방식

이 통합은 두 가지 경로를 사용합니다.

### 1. Refoss Cloud HTTP API

Refoss 계정 로그인과 과거 사용량 조회에 사용합니다.

#### 로그인

Refoss Cloud 로그인 후 다음 값을 확보합니다.

- `token`
- `key`
- API domain

여기서 `key`는 EM06 로컬 `/public` API 요청 서명에 사용됩니다.

로컬 API가 `ERROR 5001 / sign error`를 반환하면 Cloud login/key를 다시 갱신하고 같은 요청을 1회 재시도합니다.

#### 과거 1시간 사용량

검침 시작일부터 어제까지의 과거 사용량은 Refoss Cloud HTTP history API를 사용합니다.

```text
/historage/v1/deviceTelemetry/query
```

조회 조건:

```text
metric: electricH
queryType: stepSum
step: 1h
```

이 데이터는 Home Assistant 시작 또는 통합 재설정 시 ledger를 재구성하는 데 사용됩니다.

### 2. EM06 로컬 `/public` API

현재값과 최근 시간대 사용량 조회에 사용합니다.

#### ElectricityX

다음 값은 `Appliance.Control.ElectricityX`로 조회합니다.

- `mConsume`
- `power`
- `voltage`
- `factor`
- `current`

`ElectricityX`는 한 번의 요청으로 6채널 전체를 조회합니다.

```text
channel: 65535
```

#### ConsumptionH

다음 값은 `Appliance.Control.ConsumptionH`로 조회합니다.

- 채널별 오늘 사용량
- 최근 시간대별 사용량 일부
- 오늘 완료된 시간대 row 보충

`ConsumptionH`는 채널별로 조회하되, 현재 코드는 아래 순서로 나누어 요청합니다.

```text
1차 batch: 1, 2, 3 = A1, B1, C1
2차 batch: 4, 5, 6 = A2, B2, C2
```

각 batch 사이에는 짧은 대기 시간이 있습니다.

```text
CONSUMPTIONH_BATCH_DELAY = 0.1초
```

로컬 API timeout은 3초입니다.

```text
LOCAL_API_TIMEOUT = 3초
```

로컬 요청은 최대 3회 재시도합니다.

## 채널 매핑

EM06 원본 채널 번호는 다음과 같이 표시됩니다.

```text
1 -> A1
2 -> B1
3 -> C1
4 -> A2
5 -> B2
6 -> C2
```

## 생성되는 센서

예: `A1` 채널

```text
Refoss EM06 A1 Billing month energy
Refoss EM06 A1 This Day Energy
Refoss EM06 A1 Power
Refoss EM06 A1 Voltage
Refoss EM06 A1 PF
Refoss EM06 A1 Current
```

코드상 센서는 채널별로 생성됩니다.


## Entity ID

기본 이름을 `EM06`으로 설정한 경우 기대되는 entity_id 예시는 다음과 같습니다.

```text
sensor.refoss_em06_a1_billing_month_energy
sensor.refoss_em06_a1_this_day_energy
sensor.refoss_em06_a1_power
sensor.refoss_em06_a1_voltage
sensor.refoss_em06_a1_pf
sensor.refoss_em06_a1_current
```

Home Assistant는 한 번 생성된 엔티티의 `entity_id`를 엔티티 레지스트리에 저장합니다. 따라서 코드의 이름을 바꿔도 기존 `entity_id`는 자동 변경되지 않을 수 있습니다.

기존 entity_id를 유지하고 싶다면 Home Assistant의 엔티티 레지스트리에서 직접 이름만 수정하거나, 대시보드 카드에서 기존 entity_id를 계속 사용하면 됩니다.

## Ledger 저장 방식

이 통합은 완료된 시간대별 사용량을 Home Assistant `.storage`에 저장합니다.

저장 위치:

```text
/config/.storage/refoss_cloud_hourly_ledger_<uuid>
```

ledger에는 다음 row가 저장됩니다.

- Cloud HTTP history에서 가져온 검침 시작일 ~ 어제까지의 1시간 단위 row
- 시작 또는 재시작 시 로컬 `ConsumptionH`에서 확인된 오늘의 완료 row
- 운영 중 `ConsumptionH`의 두 번째 최신 row가 아직 ledger에 없을 때 추가한 row

운영 중 추가 로직은 `ConsumptionH`의 최신 row가 아직 진행 중인 시간대일 수 있다는 점을 고려해, 최신 row가 아니라 **두 번째 최신 row**를 저장 대상으로 사용합니다.

정전 등으로 EM06 자체가 측정하지 못한 구간은 임의로 0으로 채우지 않습니다.

```text
데이터가 있는 시간대 -> ledger row 저장
데이터가 없는 시간대 -> row 없음
```

따라서 나중에 실제 0 사용량과 데이터 누락을 구분할 수 있습니다.

## 계산 방식

### Billing month energy

`Billing month energy`는 검침일 기준 월 사용량입니다.

현재 로직은 다음과 같습니다.

```text
검침 시작일 ~ 어제 -> ledger에 저장된 완료 row 합산
오늘               -> 로컬 ConsumptionH의 오늘 row 합산
```

즉 최종 계산은 다음과 같습니다.

```text
Billing month energy =
  completed_history_kwh
  + today_kwh
```

검침일 당일에는 검침 시작일이 오늘이므로, 과거 ledger row가 없으면 월 사용량과 오늘 사용량이 같게 보일 수 있습니다.

### This Day Energy

`This Day Energy`는 오늘 날짜의 `ConsumptionH` row를 합산한 값입니다.

```text
This Day Energy = 오늘 ConsumptionH row 합계
```

태양광 채널처럼 순사용량이 음수가 될 수 있는 경우도 그대로 음수로 표시합니다.

### 값이 unavailable이 되는 경우

다음 경우에는 해당 센서가 일시적으로 unavailable이 될 수 있습니다.

- 로컬 API 응답이 없음
- `ElectricityX` 또는 `ConsumptionH` 일부 채널 누락
- 오늘 row와 ledger row가 모두 없음
- EM06 또는 네트워크가 일시적으로 응답하지 않음

통합은 이런 상황에서도 Home Assistant 플랫폼 설정 자체가 중단되지 않도록 설계되어 있습니다.

## 단위 변환

Refoss 원본 응답값은 아래와 같이 Home Assistant 표시 단위로 변환됩니다.

```text
mConsume / 1000 -> kWh
power    / 1000 -> W
voltage  / 1000 -> V
current  / 1000 -> A
factor          -> PF
```

에너지 센서는 kWh 기준 소수점 이하 3자리로 표시합니다.

## 설치 방법

### HACS 설치

1. Home Assistant에서 HACS를 엽니다.
2. 우측 상단 메뉴에서 `Custom repositories`를 선택합니다.
3. 아래 주소를 추가합니다.

```text
https://github.com/af950833/refoss_cloud
```

4. Category는 `Integration`을 선택합니다.
5. `Refoss Cloud`를 설치합니다.
6. Home Assistant를 재시작합니다.

### 수동 설치

다음 폴더를 Home Assistant의 `custom_components/refoss_cloud` 아래에 복사합니다.

```text
custom_components/refoss_cloud/
  __init__.py
  config_flow.py
  manifest.json
  sensor.py
  strings.json
```

복사 후 Home Assistant를 재시작합니다.

## 설정 방법

1. Home Assistant에서 `설정` > `기기 및 서비스`로 이동합니다.
2. `통합 구성요소 추가`를 선택합니다.
3. `Refoss Cloud`를 검색합니다.
4. Refoss 계정 이메일과 비밀번호를 입력합니다.
5. 계정에 연결된 EM06을 선택합니다.
6. 아래 항목을 설정합니다.
   - 이름
   - 검침일
   - 채널
   - 업데이트 주기
   - 로컬 IP 주소, 필요한 경우

## 옵션

설치 후 옵션 화면에서 업데이트 주기를 변경할 수 있습니다.

- 기본: 15초
- 최소: 10초

옵션을 저장하면 통합이 다시 로드됩니다.

## 주요 속성

`Billing month energy`와 `This Day Energy`에는 계산 확인용 속성이 포함됩니다.

### Billing month energy 속성 예시

- `source`
- `error`
- `current_mconsume_kwh`
- `completed_history_kwh`
- `today_kwh`
- `consumptionh_total_kwh`
- `ledger_row_count`
- `today_row_count`
- `period_start`
- `period_end`
- `reading_day`
- `channel`
- `channel_label`

### This Day Energy 속성 예시

- `source`
- `error`
- `today_row_count`
- `raw_total_kwh`
- `channel`
- `channel_label`

## 에너지 대시보드

에너지 센서는 아래 속성을 사용합니다.

```text
device_class: energy
state_class: total
unit_of_measurement: kWh
last_reset: 자동 계산
```

`Billing month energy`는 현재 검침월 시작 시각을 `last_reset`으로 사용합니다.

`This Day Energy`는 오늘 00:00을 `last_reset`으로 사용합니다.

## 문제 확인 포인트

값이 이상할 때는 아래를 먼저 확인해 보세요.

### 센서가 생성되지 않는 경우

- Home Assistant 로그에서 `Refoss sensor setup started` 확인
- Home Assistant 로그에서 `Refoss sensor setup completed` 확인
- `.storage/refoss_cloud_hourly_ledger_<uuid>` 파일 생성 여부 확인
- Refoss 계정 로그인 성공 여부 확인
- EM06 로컬 IP가 올바른지 확인

### `sign error`가 나오는 경우

로컬 `/public` API 요청에 필요한 Cloud login `key`가 없거나 만료된 상태일 수 있습니다.

현재 코드는 `sign error` 감지 시 Cloud login/key를 갱신하고 같은 요청을 재시도합니다.

### `Connection reset by peer` 또는 timeout이 보이는 경우

EM06 로컬 웹서버가 일부 요청 연결을 끊거나 늦게 응답하는 경우입니다.

현재 코드는 다음 방식으로 완화합니다.

- `ConsumptionH`를 1,2,3 → 4,5,6 배치로 나누어 요청
- 로컬 API timeout 3초 적용
- 요청 최대 3회 재시도
- 일부 채널 실패 시에도 가능한 채널 값은 유지

### 오늘 사용량이 이상한 경우

- `today_kwh`
- `today_row_count`
- `consumptionh_total_kwh`
- 현재 시간이 자정 직후인지
- EM06가 정전 또는 재부팅된 적이 있는지

### 월 사용량이 이상한 경우

- `period_start`
- `period_end`
- `completed_history_kwh`
- `ledger_row_count`
- `.storage/refoss_cloud_hourly_ledger_<uuid>`의 row 수

### 정전 구간이 있는 경우

EM06 자체가 정전으로 측정하지 못한 구간은 로컬 `ConsumptionH`와 Cloud history 양쪽에 데이터가 없을 수 있습니다.

이 통합은 그런 구간을 임의로 0으로 채우지 않습니다.

## 참고 사항

- 이 통합은 비공식 통합입니다.
- Refoss가 공식 문서로 공개한 API가 아니므로, 서버 응답 형식이 바뀌면 수정이 필요할 수 있습니다.
- 현재 구현은 EM06 기준입니다.
- 현재 구현은 Refoss Cloud HTTP API와 EM06 로컬 `/public` API를 사용합니다.
- Cloud MQTT는 현재 코드에서 사용하지 않습니다.

## 면책

이 프로젝트는 Refoss의 공식 제품이 아니며, Refoss와 직접적인 관련이 없습니다.
사용 중 발생하는 문제에 대해 공식 지원을 제공하지 않습니다.
