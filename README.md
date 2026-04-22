# Refoss Cloud for Home Assistant

Refoss Cloud는 Refoss EM06 전력량계를 Home Assistant에서 사용하기 위한 커스텀 통합 구성요소입니다.

Refoss 클라우드 계정으로 로그인해 EM06 데이터를 조회하고, 채널별 검침월 순사용량, 오늘 순사용량, 현재 전력, 전압, 역률, 전류 센서를 생성합니다.

이 통합은 매월 1일이 아닌 별도 검침일을 사용하는 환경을 위해 만들었습니다. 태양광 패널이 연결된 채널처럼 순사용량이 음수가 될 수 있는 경우도 그대로 표시합니다.

## 주요 기능

- Home Assistant GUI 설정 흐름 지원
- Refoss 계정 로그인 및 EM06 장치 선택
- EM06 채널 선택
  - `A1`
  - `B1`
  - `C1`
  - `A2`
  - `B2`
  - `C2`
- 채널별 검침월 순사용량 센서
- 채널별 오늘 순사용량 센서
- 채널별 현재값 센서
  - Power
  - Voltage
  - PF
  - Current
- 검침일 선택
  - `1`~`27`
  - `Last day`
- MQTT 조회 주기 설정
  - 기본값: `15초`
  - 최소값: `10초`
- Home Assistant 에너지 대시보드 등록을 위한 `state_class: total` 및 `last_reset` 지원
- 로컬 EM06 `/public` API를 사용하지 않고 Refoss cloud API와 cloud MQTT만 사용

## 데이터 조회 방식

이 통합은 두 가지 cloud 경로를 사용합니다.

### Cloud MQTT

다음 값은 Refoss cloud MQTT로 조회합니다.

- `Appliance.Control.ElectricityX`
  - `mConsume`
  - `power`
  - `voltage`
  - `factor`
  - `current`
- `Appliance.Control.ConsumptionH`
  - 오늘 순사용량 `total`
  - 최근 시간대별 사용량 `data`

현재 구현은 MQTT를 계속 subscribe해서 push를 받는 방식이 아닙니다. Refoss cloud MQTT broker에 접속한 뒤 GET 요청을 publish하고, 같은 `messageId`의 `GETACK` 응답을 받는 요청/응답 방식입니다.

### Cloud HTTP History

과거 일별 사용량은 Refoss cloud HTTP history API로 조회합니다.

```text
/historage/v1/deviceTelemetry/query
```

조회 조건:

```text
metric: electricH
queryType: stepSum
step: 1d
```

HTTP history는 검침월의 완료된 과거 날짜 합계를 계산하는 용도로만 사용합니다.

## 사용량 계산 방식

### Billing Month Energy

`Billing month energy`는 검침일 기준 순사용량입니다.

현재 계산식은 다음과 같습니다.

```text
Billing month energy =
  HTTP daily history 합계(검침 시작일 ~ 어제)
  + 어제 날짜만 ConsumptionH.data 합계로 보정
  + 오늘 ConsumptionH.total
```

이 방식은 Refoss HTTP daily history가 최근 날짜를 늦게 반영하는 경우를 줄이기 위한 구조입니다.

- 오래된 날짜: HTTP daily history 사용
- 어제: `ConsumptionH.data`에 있으면 그 합계로 보정
- 오늘: `ConsumptionH.total` 사용

`mConsume`은 현재 속성에 디버깅용으로 표시되지만, `Billing month energy`의 주 계산 기준으로 사용하지 않습니다.

### This Day Energy

`This Day Energy`는 앱의 오늘 사용량과 같은 계열인 `ConsumptionH.total`을 사용합니다.

`ConsumptionH.total`이 아직 전날 데이터처럼 보이는 경우에는 경계 시점의 튐을 줄이기 위해 오늘값을 `0`으로 처리합니다. 이 경우 속성에 다음 값이 표시됩니다.

```text
today_source: stale_cloud_mqtt_consumptionh_total
```

## 검침일 처리

검침일이 `24일`이면 예시는 다음과 같습니다.

```text
2026-04-23 23:59 -> 검침월 시작: 2026-03-24 00:00
2026-04-24 00:00 -> 검침월 시작: 2026-04-24 00:00
```

검침일이 `Last day`이면 각 달의 실제 말일을 사용합니다.

예:

- 2월 28일
- 윤년 2월 29일
- 4월 30일
- 5월 31일

## HTTP History 캐시

HTTP daily history는 매번 호출하지 않고 캐시합니다.

갱신 시점:

- Home Assistant 시작 후 첫 업데이트
- 매일 로컬 시간 `00:00:05` 이후 첫 업데이트
- 매일 로컬 시간 `00:05:00` 이후 첫 업데이트

`00:05:00` 재조회는 Refoss cloud의 daily history 반영 지연을 한 번 더 보정하기 위한 것입니다.

MQTT `ElectricityX`와 `ConsumptionH`는 설정된 업데이트 주기마다 조회합니다.

## 생성되는 센서

선택한 채널마다 다음 센서가 생성됩니다.

예: `A1` 채널

```text
Refoss EM06 A1 Billing month energy
Refoss EM06 A1 This Day Energy
Refoss EM06 A1 Power
Refoss EM06 A1 Voltage
Refoss EM06 A1 PF
Refoss EM06 A1 Current
```

## 단위 변환

Refoss 응답값은 다음과 같이 Home Assistant 표시 단위로 변환됩니다.

```text
mConsume / 1000 -> kWh
power    / 1000 -> W
voltage  / 1000 -> V
current  / 1000 -> A
factor          -> PF
```

센서 값은 소수점 아래 3자리까지 표시합니다.

## 에너지 대시보드

`Billing month energy`와 `This Day Energy`는 에너지 센서로 생성됩니다.

```text
device_class: energy
state_class: total
unit: kWh
```

`last_reset`은 자동으로 계산됩니다.

- `Billing month energy`: 현재 검침월 시작 시각
- `This Day Energy`: 오늘 00:00

검침일 또는 날짜가 바뀌면 다음 센서 업데이트 시점에 `last_reset`도 자동으로 바뀝니다.

## 음수 사용량

이 통합은 순사용량을 표시합니다.

태양광 패널이 연결된 채널처럼 생산량이 소비량보다 큰 경우 `Billing month energy` 또는 `This Day Energy`가 음수로 표시될 수 있습니다. 코드에서 `abs()` 처리를 하지 않습니다.

## 설치 방법

### HACS로 설치

1. Home Assistant에서 **HACS**를 엽니다.
2. 우측 상단 메뉴에서 **Custom repositories**를 선택합니다.
3. 저장소 주소에 다음 URL을 입력합니다.

```text
https://github.com/af950833/refoss_cloud
```

4. Category는 **Integration**을 선택합니다.
5. 저장소를 추가한 뒤 HACS에서 **Refoss Cloud**를 설치합니다.
6. Home Assistant를 재시작합니다.

### 수동 설치

Home Assistant 설정 폴더 아래에 다음 구조로 복사합니다.

```text
config/
  custom_components/
    refoss_cloud/
      __init__.py
      config_flow.py
      manifest.json
      sensor.py
      strings.json
```

복사 후 Home Assistant를 재시작합니다.

## 설정 방법

1. Home Assistant에서 **설정** > **기기 및 서비스**로 이동합니다.
2. **통합 구성요소 추가**를 선택합니다.
3. **Refoss Cloud**를 검색합니다.
4. Refoss 계정 이메일과 비밀번호를 입력합니다.
5. EM06 장치를 선택합니다.
6. 다음 항목을 설정합니다.
   - 이름
   - 검침일
   - 채널
   - MQTT 업데이트 주기

## 옵션

통합을 추가한 뒤 옵션 화면에서 MQTT 업데이트 주기를 변경할 수 있습니다.

- 기본값: `15초`
- 최소값: `10초`

옵션 변경 후 통합은 자동으로 reload되어 새 주기를 반영합니다.

## Retry 정책

API 호출은 최대 3회 시도합니다.

```text
1차 시도
실패 -> 2초 대기
2차 시도
실패 -> 2초 대기
3차 시도
실패 -> 에러 처리
```

대상:

- Cloud MQTT `ElectricityX`
- Cloud MQTT `ConsumptionH`
- Cloud HTTP history/login/device API

즉, 재시도는 2회이고 총 시도 횟수는 3회입니다.

## 디버깅 속성

`Billing month energy` 속성에서 계산 경로를 확인할 수 있습니다.

```text
source
current_mconsume_kwh
completed_history_kwh
today_consumptionh_kwh
recent_history_adjustment_kwh
history_rows
today_history_rows
today_source
period_start
period_end
```

주요 의미:

- `completed_history_kwh`: 검침 시작일~어제까지의 완료된 과거 사용량
- `today_consumptionh_kwh`: 오늘 `ConsumptionH.total`
- `recent_history_adjustment_kwh`: 어제 HTTP daily history 값을 `ConsumptionH.data`로 보정한 차이
- `current_mconsume_kwh`: 현재 EM06 mConsume 값. 계산 확인용 속성입니다.

## 문제 해결

### 센서가 unavailable로 표시되는 경우

다음을 확인하세요.

- Refoss 계정 이메일과 비밀번호가 올바른지
- Refoss 앱에서 EM06가 온라인인지
- Home Assistant가 인터넷에 연결되어 있는지
- 네트워크에서 Refoss cloud MQTT broker에 접속 가능한지

### 에너지 대시보드에 등록되지 않는 경우

Home Assistant를 재시작한 뒤 해당 센서의 속성을 확인하세요.

필요 조건:

```text
device_class: energy
state_class: total
unit_of_measurement: kWh
last_reset 존재
```

기존에 잘못된 통계 메타데이터가 남아 있으면 Home Assistant 통계 설정에서 해당 센서의 통계 문제를 정리해야 할 수 있습니다.

### 검침월 사용량이 이상한 경우

다음 속성을 확인하세요.

```text
completed_history_kwh
today_consumptionh_kwh
recent_history_adjustment_kwh
today_source
history_rows
today_history_rows
period_start
period_end
```

경계 시점에는 `00:00:05` 또는 `00:05:00` 이후 HTTP daily history 캐시가 갱신되면서 값이 보정될 수 있습니다.

## 참고 사항

- 이 통합은 Refoss EM06 기준으로 만들었습니다.
- Refoss cloud API와 cloud MQTT를 사용합니다.
- EM06 로컬 `/public` 엔드포인트는 사용하지 않습니다.
- Refoss가 공식 문서로 공개한 API가 아니므로, 앱 또는 클라우드 구조가 바뀌면 통합 수정이 필요할 수 있습니다.

## 면책

이 통합은 비공식 통합입니다. Refoss에서 공식적으로 제공하거나 보증하는 통합이 아닙니다.
