# refoss_cloud
Home Assistant Refoss Cloud custom component

# Refoss Cloud for Home Assistant

Refoss Cloud는 Refoss EM06 전력량계를 Home Assistant에서 사용하기 위한
커스텀 통합 구성요소입니다.

Refoss 클라우드에서 EM06 채널 데이터를 가져와 검침일 기준 월 사용량,
현재 전력, 전압, 역률, 전류 센서를 Home Assistant에 생성합니다.

이 통합은 전기 검침일이 매월 1일이 아닌 가정에서 사용하기 위해 만들었습니다.
또한 순사용량 값을 그대로 사용하므로, 태양광 패널이 연결된 채널처럼 역방향
전력이 발생하는 경우 월 사용량이 음수로 표시될 수 있습니다.

## 주요 기능

- Refoss 계정 로그인을 위한 GUI 설정 흐름
- Refoss 클라우드 계정의 EM06 기기 선택
- EM06 채널 선택
  - `A1`
  - `B1`
  - `C1`
  - `A2`
  - `B2`
  - `C2`
- 선택한 채널별 검침월 순사용량 센서
- 선택한 채널별 현재값 센서
  - 현재 전력
  - 전압
  - 역률
  - 전류
- 검침일 선택
  - `1일`부터 `27일`
  - `말일`
- MQTT 조회 주기 설정
  - 기본값: `15초`
  - 최소값: `10초`
- 클라우드 전용 조회
  - EM06 로컬 `/public` 엔드포인트는 사용하지 않습니다.

## 데이터 조회 방식

이 통합은 Refoss 클라우드에서 두 종류의 데이터를 가져옵니다.

## Cloud MQTT

현재값은 Cloud MQTT로 조회합니다.

- `mConsume`
- `power`
- `voltage`
- `factor`
- `current`

로그인 API에서 받은 Refoss cloud MQTT broker로 접속한 뒤
`Appliance.Control.ElectricityX`를 요청합니다.

예시 broker:

```text
mqtt-ap-v2.refoss.net
```

현재 구현은 MQTT를 계속 구독해 push를 받는 방식이 아니라, 앱처럼 MQTT로
GET 요청을 보내고 GETACK 응답을 받는 요청/응답 방식입니다.

## Cloud HTTP History

HTTP history는 과거 일별 보정값을 계산할 때만 사용합니다.

조회 엔드포인트:

```text
/historage/v1/deviceTelemetry/query
```

조회 조건:

```text
metric: electricH
queryType: stepSum
step: 1d
```

현재값은 MQTT에서 가져오고, HTTP history는 검침월 계산에 필요한 과거 일별
합계를 보정하는 용도로만 사용합니다.

## 검침월 사용량 계산 방식

Refoss의 `mConsume`은 해당 월 1일부터 현재까지의 월간 순누적값으로 보고
사용합니다. 이 값을 사용자가 설정한 검침일 기준으로 보정합니다.

## 검침일이 이번 달에 이미 지난 경우

예를 들어 오늘이 4월 27일이고 검침일이 4월 24일이라면:

```text
검침월 사용량 = MQTT 현재 mConsume - HTTP history 4/1~4/23 사용량
```

결과적으로 남는 기간:

```text
4/24 00:00부터 현재까지
```

## 검침일이 아직 오지 않은 경우

예를 들어 오늘이 4월 20일이고 검침일이 4월 24일이라면:

```text
검침월 사용량 =
  HTTP history 3/24~3/31 사용량
  + MQTT 현재 mConsume
```

결과적으로 계산되는 기간:

```text
3/24 00:00부터 현재까지
```

## 말일 검침

검침일을 `말일`로 선택하면 각 월의 실제 마지막 날을 사용합니다.

예:

- 2월 28일
- 윤년 2월 29일
- 4월 30일
- 5월 31일

현재 날짜가 이번 달 말일 전이면 전월 말일부터 계산하고, 이번 달 말일 이후면
이번 달 말일부터 계산합니다.

## HTTP History 갱신 주기

MQTT 현재값은 설정한 조회 주기마다 갱신합니다.

HTTP history는 매번 조회하지 않고 캐시합니다. 갱신 시점은 다음과 같습니다.

- Home Assistant 시작 후 첫 업데이트
- 매일 로컬 시간 `00:00:05` 이후 첫 업데이트
- 매일 로컬 시간 `00:05:00` 이후 첫 업데이트

`00:05:00` 재조회는 Refoss cloud의 일별 history 반영이 늦을 수 있어 넣은
보정용 재시도입니다.

## 생성되는 센서

선택한 채널마다 검침월 사용량 센서 1개와 현재값 센서 4개가 생성됩니다.

예: `A1` 채널

```text
Refoss EM06 A1 Billing month energy
Refoss EM06 A1 Power
Refoss EM06 A1 Voltage
Refoss EM06 A1 PF
Refoss EM06 A1 Current
```

## 단위 변환

Refoss MQTT 응답값은 다음과 같이 변환합니다.

```text
mConsume / 1000 -> kWh
power    / 1000 -> W
voltage  / 1000 -> V
current  / 1000 -> A
factor          -> PF
```

센서 값은 소수점 아래 3자리까지 표시합니다.

## 음수 사용량

검침월 사용량은 순사용량입니다.

코드에서 `abs()` 처리를 하지 않습니다. 따라서 태양광 패널이 연결된 채널처럼
생산량이 소비량보다 큰 경우 검침월 사용량이 음수로 표시될 수 있습니다.

이는 의도된 동작입니다.

## 설치 방법

Home Assistant 설정 폴더 아래에 통합 폴더를 복사합니다.

```text
custom_components/refoss_cloud
```

폴더 구조 예:

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

파일을 복사한 뒤 Home Assistant를 재시작합니다.

## 설정 방법

1. Home Assistant를 엽니다.
2. **설정** > **기기 및 서비스**로 이동합니다.
3. **통합 구성요소 추가**를 선택합니다.
4. **Refoss Cloud**를 검색합니다.
5. Refoss 계정 이메일과 비밀번호를 입력합니다.
6. EM06 기기를 선택합니다.
7. 다음 항목을 설정합니다.
   - 이름
   - 검침일
   - 채널
   - MQTT 업데이트 주기

## 옵션

통합을 추가한 뒤에도 옵션 화면에서 MQTT 업데이트 주기를 변경할 수 있습니다.

최소값은 `10초`입니다.

옵션을 변경하면 통합이 자동으로 다시 로드되어 새 주기가 반영됩니다.

## 참고 사항

- 이 통합은 Refoss EM06을 기준으로 만들었습니다.
- Refoss cloud API와 cloud MQTT를 사용합니다.
- EM06 로컬 `/public` 엔드포인트는 사용하지 않습니다.
- Refoss에서 공식 문서로 공개한 API가 아니므로, 앱 또는 클라우드 구조가 바뀌면
  통합 수정이 필요할 수 있습니다.

## 문제 해결

## 센서가 unavailable로 표시되는 경우

다음을 확인하세요.

- Refoss 계정 이메일과 비밀번호가 올바른지
- Refoss 앱에서 EM06이 온라인인지
- Home Assistant가 인터넷에 연결되어 있는지
- 네트워크에서 Refoss cloud MQTT broker에 접속할 수 있는지

## 검침월 사용량이 이상한 경우

센서 속성에서 다음 값을 확인하세요.

- `current_mconsume_kwh`
- `month_prefix_kwh`
- `previous_period_kwh`
- `history_rows`
- `period_start`
- `period_end`

이 속성들은 최종 검침월 사용량이 어떻게 계산되었는지 확인하기 위한 디버그
정보입니다.

## 현재값은 변하는데 검침월 사용량이 잘 안 변하는 경우

값이 `0.001 kWh`보다 작게 변하면 소수점 3자리 반올림 때문에 화면에서는
변하지 않는 것처럼 보일 수 있습니다.

## 면책

이 통합은 비공식 통합입니다. Refoss에서 공식적으로 제공하거나 보증하는
통합이 아닙니다.
