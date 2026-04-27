# Refoss Cloud for Home Assistant

Refoss EM06을 Home Assistant에서 사용하기 위한 비공식 커스텀 통합입니다.

현재 버전은 **Refoss Cloud HTTP API**와 EM06의 **로컬 `/public` API**를 함께 사용합니다. 과거 사용량은 클라우드 history에서 가져오고, 현재 전력·전압·전류·역률과 최근 시간대 사용량은 로컬 API에서 조회합니다.

## 주요 특징

- Refoss 계정 로그인 후 EM06 데이터 조회
- EM06 6채널 지원
  - A1, B1, C1, A2, B2, C2
- 검침일 기준 월 사용량 계산
- 오늘 사용량 계산
- 현재 전력, 전압, 역률, 전류 제공
- 태양광 채널처럼 음수 사용량이 발생하는 환경 지원
- 로컬 API `sign error` 발생 시 cloud key 재로그인 후 재시도
- `ConsumptionH` 조회 시 A1/B1/C1 → A2/B2/C2 순서로 batch 처리
- 59분대 불안정 `ConsumptionH` 응답 방어
- 원장은 파일에 저장하지 않고 메모리에서만 유지

## 지원 대상

- Refoss EM06
- Home Assistant 커스텀 통합

현재 구현은 EM06 기준입니다. 다른 Refoss 장비는 동작을 보장하지 않습니다.

## 데이터 조회 구조

### 1. Refoss Cloud HTTP API

클라우드 API는 다음 용도로 사용합니다.

- Refoss 계정 로그인
- 로컬 API 서명에 필요한 `key` 확보
- 과거 1시간 단위 전력 사용량 history 조회

과거 사용량 조회 endpoint:

```text
/historage/v1/deviceTelemetry/query
```

조회 조건:

```text
metric: electricH
queryType: stepSum
step: 1h
```

### 2. EM06 로컬 `/public` API

로컬 API는 다음 namespace를 사용합니다.

```text
Appliance.Control.ElectricityX
Appliance.Control.ConsumptionH
```

`ElectricityX`는 `channel=65535`로 한 번 조회하여 6채널의 즉시값을 가져옵니다.

- 현재 전력
- 전압
- 전류
- 역률
- mConsume

`ConsumptionH`는 채널별로 조회합니다. EM06 로컬 API의 동시 요청 부담을 줄이기 위해 아래 순서로 처리합니다.

```text
1차 batch: 1, 2, 3 = A1, B1, C1
2차 batch: 4, 5, 6 = A2, B2, C2
```

각 batch 사이에는 짧은 대기 시간이 있습니다.

```text
CONSUMPTIONH_BATCH_DELAY = 0.1초
```

로컬 API timeout은 다음과 같습니다.

```text
LOCAL_API_TIMEOUT = 3초
```

## 로컬 API 인증과 sign 처리

EM06 로컬 `/public` API 요청에는 Refoss Cloud 로그인으로 받은 `key`가 필요합니다.

로컬 요청 전 `token`과 `key`가 없으면 먼저 cloud login을 수행합니다. 로컬 API에서 `sign error`가 반환되면 cloud login을 다시 수행하여 `key`를 갱신한 뒤 같은 요청을 재시도합니다.

대표적인 sign error 응답:

```json
{
  "payload": {
    "error": {
      "code": 5001,
      "detail": "sign error"
    }
  }
}
```

이 경우 통합은 자동으로 key를 갱신하고 재시도합니다.

## 메모리 원장 구조

현재 버전은 원장을 `.storage` 파일로 저장하지 않습니다. Home Assistant가 시작될 때마다 cloud history와 local `ConsumptionH`를 이용해 원장을 메모리에서 새로 구성합니다.

메모리에 저장되는 원장 구조는 다음과 같습니다.

```python
{
    channel: {
        timestamp: ConsumptionRow(
            timestamp=...,      # 초 단위 Unix timestamp
            local_dt=...,       # Home Assistant 로컬 시간
            value_wh=...,       # 해당 시간대 Wh 값
        )
    }
}
```

예시:

```python
{
    1: {
        1777301940: ConsumptionRow(
            timestamp=1777301940,
            local_dt="2026-04-27 23:59:00",
            value_wh=64,
        )
    }
}
```

중복 여부는 채널별 `timestamp`로 판단합니다. 같은 채널에 같은 timestamp가 이미 있으면 다시 추가하지 않습니다.

## 원장 rebuild 방식

Home Assistant 시작 시 다음 순서로 원장을 재구성합니다.

```text
1. 빈 메모리 원장 생성
2. Refoss Cloud history에서 검침 시작일 ~ 어제까지 1시간 단위 데이터 조회
3. cloud history에 없는 어제 시간대가 있으면 local ConsumptionH에 같은 날짜·시간 row가 있을 때만 보충
4. 오늘 날짜의 ConsumptionH row 중 최신 row를 제외한 완료 row를 메모리 원장에 저장
```

없는 시간대는 `0`으로 채우지 않습니다.

```text
데이터 없음       → row 없음
실제 value=0 수신 → value 0 row 저장
```

따라서 정전 등으로 EM06 자체가 측정하지 못한 구간은 원장에 저장되지 않습니다.

## 운영 중 원장 append

운영 중에는 각 polling마다 `ConsumptionH`의 두 번째 최신 row를 완료 row로 보고 원장에 추가합니다.

```text
최신 row       → 현재 진행 중인 시간대일 수 있으므로 원장에 저장하지 않음
두 번째 최신 row → 완료된 직전 시간대로 보고 원장에 저장 시도
```

단, 이미 같은 timestamp가 원장에 있으면 추가하지 않습니다.

## 사용량 계산 방식

### Billing month energy

검침일 기준 월 사용량입니다.

```text
Billing month energy =
  메모리 원장의 검침 시작일 ~ 어제까지 합계
  + 현재 ConsumptionH의 오늘 날짜 row 합계
```

검침 시작일은 설정한 검침일을 기준으로 계산합니다. 검침일을 28일 이상으로 설정하면 말일 기준으로 처리합니다.

### This Day Energy

오늘 사용량입니다.

```text
This Day Energy =
  현재 ConsumptionH 응답 중 오늘 날짜 row의 value_wh 합계
```

태양광 채널처럼 음수 row가 들어오면 그대로 합산합니다.

### 59분대 불안정 응답 방어

EM06가 매시 경계 부근에 `ConsumptionH` 최신 row만 임시로 반환하면서 오늘 사용량이 순간적으로 내려가는 경우가 있습니다.

이를 방지하기 위해 최신 row와 두 번째 최신 row의 timestamp 차이가 1시간 이상이면 해당 polling의 에너지값 갱신을 보류합니다.

```text
latest timestamp - second timestamp >= 3600초
→ This Day Energy / Billing month energy는 직전 정상값 유지
→ Power / Voltage / Current / PF는 계속 갱신
```

이때 source는 다음과 같이 표시될 수 있습니다.

```text
held_previous_energy_unstable_consumptionh_gap
```

다음 polling에서 정상 `ConsumptionH` row가 들어오면 다시 정상 계산으로 복귀합니다.

## 채널 매핑

EM06 원본 채널 번호는 다음 표시명으로 매핑됩니다.

```text
1 -> A1
2 -> B1
3 -> C1
4 -> A2
5 -> B2
6 -> C2
```

## 생성되는 센서

예: 이름을 `EM06`으로 설정하고 A1 채널을 사용할 경우:

```text
Refoss EM06 A1 Billing month energy
Refoss EM06 A1 This Day Energy
Refoss EM06 A1 Power
Refoss EM06 A1 Voltage
Refoss EM06 A1 PF
Refoss EM06 A1 Current
```

예상 entity_id 예시:

```text
sensor.refoss_em06_a1_billing_month_energy
sensor.refoss_em06_a1_this_day_energy
sensor.refoss_em06_a1_power
sensor.refoss_em06_a1_voltage
sensor.refoss_em06_a1_power_factor
sensor.refoss_em06_a1_current
```

## 단위 변환

Refoss 원본값은 아래와 같이 Home Assistant 표시 단위로 변환됩니다.

```text
ConsumptionH value      Wh 그대로 사용 후 / 1000 → kWh
mConsume                Wh 그대로 사용 후 / 1000 → kWh
power                   mW / 1000 → W
voltage                 mV / 1000 → V
current                 mA / 1000 → A
factor                  PF, 소수점 3자리 반올림
```

에너지 센서는 kWh 기준으로 소수점 이하 3자리까지 표시합니다.

## 주요 속성

에너지 센서에는 계산 확인용 속성이 포함됩니다.

```text
source
error
current_mconsume_kwh
completed_history_kwh
today_kwh
consumptionh_total_kwh
ledger_row_count
today_row_count
```

### source 값 예시

```text
ledger_plus_local_consumptionh_data
```

정상 계산 상태입니다. 의미는 다음과 같습니다.

```text
원장 데이터 + 로컬 ConsumptionH 데이터로 에너지값을 계산함
```

```text
held_previous_energy_unstable_consumptionh_gap
```

`ConsumptionH` 최신 row와 두 번째 최신 row의 간격이 1시간 이상이라 이번 polling의 에너지값 갱신을 보류하고 직전 정상값을 유지한 상태입니다.

```text
no_current_consumption_rows
```

현재 polling에서 오늘 날짜 `ConsumptionH` row가 없고, 계산할 원장 row도 부족한 상태입니다.

```text
unavailable
```

업데이트 실패 등으로 해당 채널 값을 만들 수 없는 상태입니다.

### error 값

```yaml
error: null
```

정상입니다. 기록할 에러가 없다는 뜻입니다.

문제가 있을 때는 다음과 같은 값이 들어갈 수 있습니다.

```text
ElectricityX missing
ConsumptionH missing
ConsumptionH unstable latest gap 3600s
```

## 설치 방법

### HACS 설치

1. Home Assistant에서 HACS를 엽니다.
2. 우측 상단 메뉴에서 `Custom repositories`를 선택합니다.
3. 저장소 주소를 추가합니다.

```text
https://github.com/af950833/refoss_cloud
```

4. Category는 `Integration`을 선택합니다.
5. `Refoss Cloud`를 설치합니다.
6. Home Assistant를 재시작합니다.

### 수동 설치

다음 파일을 Home Assistant의 `custom_components/refoss_cloud` 아래에 복사합니다.

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

1. Home Assistant에서 `설정` → `기기 및 서비스`로 이동합니다.
2. `통합 구성요소 추가`를 선택합니다.
3. `Refoss Cloud`를 검색합니다.
4. Refoss 계정 이메일과 비밀번호를 입력합니다.
5. EM06 장치를 선택합니다.
6. 아래 항목을 설정합니다.
   - 이름
   - 검침일
   - 채널
   - 업데이트 주기
   - 로컬 host, 필요한 경우

## 옵션

기본 업데이트 주기:

```text
15초
```

최소 업데이트 주기:

```text
10초
```

옵션을 저장하면 통합이 다시 로드됩니다.

## 에너지 대시보드

에너지 센서는 아래 속성을 사용합니다.

```text
device_class: energy
state_class: total
unit_of_measurement: kWh
```

`Billing month energy`의 `last_reset`은 현재 검침월 시작 시각입니다.  
`This Day Energy`의 `last_reset`은 오늘 00:00입니다.

## 문제 확인 포인트

### 센서가 생성되지 않는 경우

- Refoss 계정 정보가 맞는지 확인
- EM06 UUID가 맞는지 확인
- 로컬 host가 자동 검색되지 않으면 직접 IP 입력
- Home Assistant 로그에서 `sign error`, `no GETACK`, `Connection reset by peer` 확인

### 오늘 사용량이 순간적으로 내려가는 경우

현재 버전은 latest-second gap 방어 로직을 포함합니다. 로그에서 다음 source가 보이면 정상적으로 방어한 것입니다.

```text
held_previous_energy_unstable_consumptionh_gap
```

### 월사용량이 예상보다 작게 보이는 경우

아래 속성을 확인합니다.

```text
period_start
completed_history_kwh
today_kwh
ledger_row_count
today_row_count
```

정전 등으로 EM06가 측정하지 못한 시간은 `0`으로 채워지지 않습니다. 해당 시간대 row가 없으면 합산에도 포함되지 않습니다.

### 전압, 전류, 전력은 정상인데 사용량만 이상한 경우

전압·전류·전력·역률은 `ElectricityX`에서 가져오고, 사용량은 `ConsumptionH`와 원장 계산을 사용합니다. 따라서 순간값은 정상인데 에너지값만 보류되는 상황이 있을 수 있습니다.

## 디버그 로그

문제 분석 시 다음 로거를 debug로 설정하면 도움이 됩니다.

```yaml
logger:
  default: warning
  logs:
    custom_components.refoss_cloud: debug
```

대표 로그:

```text
Refoss local snapshot started
Refoss local ConsumptionH batch started
Refoss ConsumptionH parsed
Refoss in-memory ledger rebuild completed
Refoss ConsumptionH energy update ignored due to unstable latest gap
Refoss channel update
```

## 참고 사항

- 이 통합은 비공식 통합입니다.
- Refoss가 공식 문서로 공개한 API가 아니므로 서버 응답 형식이 바뀌면 수정이 필요할 수 있습니다.
- 현재 구현은 EM06 기준입니다.
- 원장은 메모리에만 유지되므로 Home Assistant 재시작 시 다시 rebuild됩니다.
- 이전 버전에서 생성된 `.storage/refoss_cloud_hourly_ledger_...` 파일이 남아 있어도 현재 버전에서는 사용하지 않습니다.

## 면책

이 프로젝트는 Refoss의 공식 제품이 아니며, Refoss와 직접적인 관련이 없습니다. 사용 중 발생하는 문제에 대해 공식 지원을 제공하지 않습니다.
