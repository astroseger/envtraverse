FROM gradle:8.10.2-jdk21@sha256:963d59f7f22767da4efbcf46b661361b61af5fb88b0309da1071c4234c647eba AS build
WORKDIR /build
COPY settings.gradle build.gradle gradle.properties ./
COPY src ./src
RUN --mount=type=cache,target=/build-cache gradle --gradle-user-home /build-cache --no-daemon build

FROM eclipse-temurin:21-jre-jammy@sha256:dff6eb46527bd293a3c06bbbe9e64e8c9f91f01905b5abbdad31761c03059d2c
ARG MINECRAFT_VERSION=1.21.1
ARG FABRIC_LOADER_VERSION=0.16.10
ARG FABRIC_INSTALLER_VERSION=1.0.1
ARG FABRIC_API_VERSION=0.102.1+1.21.1
RUN apt-get update && apt-get install -y --no-install-recommends curl python3 ca-certificates && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/server/mods && \
    curl --retry 3 --retry-delay 2 --retry-all-errors -fsSL "https://meta.fabricmc.net/v2/versions/loader/${MINECRAFT_VERSION}/${FABRIC_LOADER_VERSION}/${FABRIC_INSTALLER_VERSION}/server/jar" -o /opt/server/fabric-server-launch.jar && \
    curl --retry 3 --retry-delay 2 --retry-all-errors -fsSL "https://maven.fabricmc.net/net/fabricmc/fabric-api/fabric-api/${FABRIC_API_VERSION}/fabric-api-${FABRIC_API_VERSION}.jar" -o /opt/server/mods/fabric-api.jar
# Install game libraries before source-dependent COPY layers. The build stops
# at eula=false; each fresh volume inherits code caches, never world/player data.
COPY prefetch.py /opt/server/prefetch.py
RUN python3 /opt/server/prefetch.py --minecraft "$MINECRAFT_VERSION" --loader "$FABRIC_LOADER_VERSION"
COPY --from=build /build/build/libs/lifemc-observer-0.1.0.jar /opt/server/mods/lifemc-observer.jar
COPY start.py /opt/server/start.py
LABEL io.envtraverse.life_minecraft.prepared="1"
WORKDIR /data
EXPOSE 25565
ENTRYPOINT ["python3", "/opt/server/start.py"]
