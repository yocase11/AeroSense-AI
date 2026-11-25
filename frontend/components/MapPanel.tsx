"use client";

import { MapContainer, CircleMarker, Popup, TileLayer } from "react-leaflet";

type MapPoint = {
  id: string | number;
  latitude: number;
  longitude: number;
  title: string;
  detail?: string;
  kind: "station" | "event";
};

export default function MapPanel({ points }: { points: MapPoint[] }) {
  const center: [number, number] = points.length
    ? [points[0].latitude, points[0].longitude]
    : [20.5937, 78.9629];
  return (
    <div className="map-frame">
      <MapContainer center={center} zoom={points.length ? 7 : 5} scrollWheelZoom>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        {points.map((point) => (
          <CircleMarker
            key={`${point.kind}-${point.id}`}
            center={[point.latitude, point.longitude]}
            radius={point.kind === "station" ? 7 : 9}
            pathOptions={{
              color: point.kind === "station" ? "#57d6b1" : "#ff9f70",
              fillOpacity: 0.8,
            }}
          >
            <Popup>
              <strong>{point.title}</strong>
              {point.detail && <div>{point.detail}</div>}
            </Popup>
          </CircleMarker>
        ))}
      </MapContainer>
    </div>
  );
}
